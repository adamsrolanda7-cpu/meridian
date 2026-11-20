"""Providers de l'agent.

- ``mock`` : routeur d'intentions déterministe, sans clé API, pour tester le
  CLI et les outils hors ligne.
- ``openai`` : client minimal OpenAI-compatible (OpenRouter, Groq, Ollama,
  OpenAI, etc.) avec tool calling et fallback de modèles.

Le provider réel ne reçoit jamais de clé d'exchange. Il ne peut appeler que
les outils exposés par le registre.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import formatting


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        self.retryable = retryable
        self.status = status
        super().__init__(message)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderResponse:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None


class Provider:
    name = "base"

    def begin_turn(self, user_text: str) -> None:
        """Hook appelé au début de chaque tour utilisateur."""

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        raise NotImplementedError


# --------------------------------------------------------------------- helpers
def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    return " ".join(
        text.lower().replace("'", " ").replace("’", " ").replace("`", " ").split()
    )


def _has(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _detect_metric(text: str) -> str | None:
    aliases = (
        ("max_drawdown", ("max drawdown", "maximum drawdown", "drawdown maximal", "max dd", "dd max", "perte maximale")),
        ("drawdown", ("drawdown", "sous l eau", "underwater")),
        ("sharpe", ("sharpe",)),
        ("sortino", ("sortino",)),
        ("calmar", ("calmar",)),
        ("cagr", ("cagr", "rendement annualise", "croissance annuelle", "rendement annuel")),
        ("volatility", ("volatilite", "volatility")),
        ("turnover_x", ("turnover", "rotation")),
        ("hit_rate", ("hit rate", "taux de reussite", "win rate")),
        ("profit_factor", ("profit factor", "facteur de profit")),
        ("expectancy", ("expectancy", "esperance", "pnl moyen")),
        ("exposure", ("exposition", "exposure", "gross exposure")),
        ("beta", ("beta",)),
        ("correlation", ("correlation",)),
    )
    for key, needles in aliases:
        if any(needle in text for needle in needles):
            return key
    return None


def _detect_strategies(text: str) -> list[str]:
    """Détecte les stratégies et conserve l'ordre d'apparition dans le texte."""
    matches: list[tuple[int, str]] = []
    mean_needles = (
        "mean reversion",
        "mean-reversion",
        "meanrev",
        "retour a la moyenne",
        "retour vers la moyenne",
        "reversion",
    )
    mean_positions = [text.find(needle) for needle in mean_needles if needle in text]
    if mean_positions:
        matches.append((min(mean_positions), "mean_reversion"))
    trend_needles = ("trend", "tendance", "momentum", "xsmom", "suivi de tendance")
    trend_positions = [
        text.find(needle) for needle in trend_needles if needle in text
    ]
    if trend_positions:
        matches.append((min(trend_positions), "trend_momentum"))
    matches.sort()
    return [name for _, name in matches]


def _date_args(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    iso = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if iso:
        out["start"] = iso[0]
        if len(iso) > 1:
            out["end"] = iso[1]
        return out
    range_match = re.search(
        r"\b(20\d{2})\s*(?:-|a|au|to)\s*(20\d{2})\b", text
    )
    if range_match:
        out["start"] = f"{range_match.group(1)}-01-01"
        out["end"] = f"{range_match.group(2)}-12-31"
        return out
    since_match = re.search(
        r"\b(?:depuis|from|a partir de)\s+(20\d{2})\b", text
    )
    if since_match:
        out["start"] = f"{since_match.group(1)}-01-01"
        return out
    year_match = re.search(r"\b(?:en|in)\s+(20\d{2})\b", text)
    if year_match:
        out["start"] = f"{year_match.group(1)}-01-01"
        out["end"] = f"{year_match.group(1)}-12-31"
        return out
    bare_year = re.search(r"\b(20\d{2})\b", text)
    if bare_year:
        out["start"] = f"{bare_year.group(1)}-01-01"
        out["end"] = f"{bare_year.group(1)}-12-31"
        return out
    lookback = re.search(
        r"\b(\d{1,4})\s*(ans?|annees?|years?|mois|months?|semaines?|weeks?|jours?|days?)\b",
        text,
    )
    if lookback:
        amount = int(lookback.group(1))
        unit = lookback.group(2)
        if unit.startswith(("an", "year")):
            days = amount * 365
        elif unit.startswith(("mois", "month")):
            days = amount * 30
        elif unit.startswith(("semaine", "week")):
            days = amount * 7
        else:
            days = amount
        out["lookback_days"] = max(30, min(3650, days))
    return out


def _is_out_of_scope(text: str) -> bool:
    return _has(
        text,
        "ordre reel",
        "ordre réel",
        "argent reel",
        "trader reel",
        "trader avec de l argent",
        "pari",
        "rendement garanti",
        "gain garanti",
        "predire le prix",
        "prediction de prix",
        "sans risque",
    )


# ------------------------------------------------------------------------- mock
class MockProvider(Provider):
    """Routeur déterministe : pas de LLM, mais le même contrat d'outils."""

    name = "mock"

    def __init__(self):
        self._phase = "idle"
        self._user_text = ""
        self._plan: dict[str, Any] | None = None
        self._turn = 0
        self._seq = 0

    def begin_turn(self, user_text: str) -> None:
        self._phase = "plan"
        self._user_text = user_text
        self._plan = None
        self._turn += 1
        self._seq = 0

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        if self._phase == "plan":
            plan = self._plan_for(_norm(self._user_text))
            self._plan = plan
            if plan["kind"] == "text":
                answer = plan["text"]
                return ProviderResponse(
                    text=answer,
                    usage=self._estimate_usage(messages, answer),
                )
            self._phase = "await_tools"
            calls = [
                self._call(name, args) for name, args in plan["calls"]
            ]
            return ProviderResponse(
                tool_calls=calls,
                usage=self._estimate_usage(messages, ""),
            )

        if self._phase == "await_tools":
            results = self._tool_results(messages)
            if self._plan and self._plan["kind"] == "compare":
                run_ids = [
                    str((item["result"].get("data") or {}).get("run_id"))
                    for item in results
                    if item["name"] == "run_backtest"
                    and item["result"].get("ok")
                    and (item["result"].get("data") or {}).get("run_id")
                ]
                if len(run_ids) < 2:
                    return ProviderResponse(
                        text=self._tool_error_text(results),
                        usage=self._estimate_usage(messages, ""),
                    )
                self._phase = "await_compare"
                return ProviderResponse(
                    tool_calls=[
                        self._call(
                            "compare_backtests",
                            {"run_id_a": run_ids[0], "run_id_b": run_ids[1]},
                        )
                    ],
                    usage=self._estimate_usage(messages, ""),
                )
            answer = self._format_results(results)
            return ProviderResponse(
                text=answer,
                usage=self._estimate_usage(messages, answer),
            )

        if self._phase == "await_compare":
            results = self._tool_results(messages)
            comparisons = [
                item for item in results if item["name"] == "compare_backtests"
            ]
            if comparisons:
                result = comparisons[-1]["result"]
                if result.get("ok"):
                    answer = formatting.format_comparison(
                        result.get("data") or {}
                    )
                else:
                    answer = (
                        "La comparaison a échoué : "
                        f"{result.get('error') or 'erreur inconnue'}"
                    )
            else:
                answer = self._tool_error_text(results)
            return ProviderResponse(
                text=answer,
                usage=self._estimate_usage(messages, answer),
            )

        return ProviderResponse(text=formatting.help_text())

    # ------------------------------------------------------------------- plan
    def _plan_for(self, text: str) -> dict[str, Any]:
        if not text:
            return {"kind": "text", "text": formatting.help_text()}
        if _is_out_of_scope(text):
            return {
                "kind": "text",
                "text": (
                    "Je ne fais pas de trading réel, de levier, de short, de "
                    "prédiction de prix ou de promesse de rendement. Je peux "
                    "seulement backtester et lire un portefeuille paper. "
                    "Demande par exemple : « Fais un backtest trend sur 2 ans »."
                ),
            }

        run_ids = re.findall(r"bt_[0-9a-f]{12}", text)
        metric = _detect_metric(text)
        wants_compare = _has(
            text, "compare", "comparer", "comparaison", "versus", " versus ", " vs "
        )
        wants_backtest = _has(
            text, "backtest", "back-test", "rejoue", "simule", "teste", "tester"
        )
        wants_latest = _has(text, "dernier", "derniere", "latest", "resultat")
        wants_list = _has(text, "strategie", "strategies") and not wants_backtest and not wants_compare
        wants_explain = _has(
            text, "explique", "expliquer", "c est quoi", "definition", "comment lire", "a quoi sert"
        )
        wants_usage = _has(
            text,
            "usage",
            "quota",
            "quotas",
            "limite",
            "limites",
            "consommation",
            "combien de backtest",
            "combien de message",
            "combien de token",
            "combien de quota",
        )
        wants_portfolio = _has(
            text,
            "portefeuille",
            "portfolio",
            "paper",
            "position",
            "positions",
            "equity",
            "cash",
        )
        wants_risk = _has(
            text,
            "risque",
            "risk",
            "drawdown",
            "monitor",
            "alerte",
            "kill",
            "veto",
        )

        date_args = _date_args(text)

        if wants_compare and len(run_ids) >= 2:
            return {
                "kind": "tools",
                "calls": [
                    (
                        "compare_backtests",
                        {"run_id_a": run_ids[0], "run_id_b": run_ids[1]},
                    )
                ],
            }

        if wants_compare:
            strategies = _detect_strategies(text)
            if not strategies:
                strategies = ["trend_momentum", "mean_reversion"]
            elif len(strategies) == 1:
                other = (
                    "mean_reversion"
                    if strategies[0] == "trend_momentum"
                    else "trend_momentum"
                )
                strategies = [strategies[0], other]
            calls = [
                ("run_backtest", {"strategy": name, **date_args})
                for name in strategies[:2]
            ]
            return {"kind": "compare", "calls": calls}

        if wants_latest and (
            wants_backtest or wants_explain or _has(text, "resultat", "resultats")
        ):
            return {"kind": "tools", "calls": [("get_backtest", {"run_id": "latest"})]}

        if run_ids:
            return {
                "kind": "tools",
                "calls": [("get_backtest", {"run_id": run_ids[0]})],
            }

        if wants_list:
            return {"kind": "tools", "calls": [("list_strategies", {})]}

        if wants_explain and metric:
            return {
                "kind": "tools",
                "calls": [("explain_metric", {"metric": metric})],
            }

        if wants_usage:
            return {"kind": "tools", "calls": [("get_usage", {})]}

        if wants_portfolio:
            return {"kind": "tools", "calls": [("get_paper_portfolio", {})]}

        if wants_risk:
            return {"kind": "tools", "calls": [("get_risk_report", {})]}

        if wants_backtest:
            strategies = _detect_strategies(text)
            if len(strategies) > 1:
                return {
                    "kind": "text",
                    "text": (
                        "Tu mentionnes deux stratégies. Pour les comparer, dis "
                        "« compare trend et mean reversion ». Pour en tester une "
                        "seule, précise « trend » ou « mean reversion »."
                    ),
                }
            strategy = strategies[0] if strategies else "trend_momentum"
            return {
                "kind": "tools",
                "calls": [
                    (
                        "run_backtest",
                        {"strategy": strategy, **date_args},
                    )
                ],
            }

        if metric and wants_explain:
            return {
                "kind": "tools",
                "calls": [("explain_metric", {"metric": metric})],
            }

        # Une métrique seule, ex. « sharpe ».
        if metric and len(text.split()) <= 3:
            return {
                "kind": "tools",
                "calls": [("explain_metric", {"metric": metric})],
            }

        return {"kind": "text", "text": formatting.help_text()}

    # ------------------------------------------------------------------ tools
    def _call(self, name: str, args: dict[str, Any]) -> ToolCall:
        self._seq += 1
        return ToolCall(
            id=f"mock_{self._turn}_{self._seq}",
            name=name,
            arguments=args,
        )

    @staticmethod
    def _tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        last_user = -1
        for index, message in enumerate(messages):
            if message.get("role") == "user":
                last_user = index
        out: list[dict[str, Any]] = []
        for message in messages[last_user + 1 :]:
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            try:
                payload = json.loads(content) if isinstance(content, str) else content
            except json.JSONDecodeError:
                payload = {"ok": False, "error": str(content)}
            if not isinstance(payload, dict):
                payload = {"ok": False, "error": str(payload)}
            out.append(
                {
                    "name": message.get("name") or payload.get("tool"),
                    "result": payload,
                }
            )
        return out

    def _tool_error_text(self, results: list[dict[str, Any]]) -> str:
        errors = [
            f"{item['name']} : {item['result'].get('error')}"
            for item in results
            if not item["result"].get("ok")
        ]
        if errors:
            return "Je n'ai pas pu terminer :\n- " + "\n- ".join(errors)
        return "Je n'ai pas obtenu de résultat exploitable."

    def _format_results(self, results: list[dict[str, Any]]) -> str:
        if not results:
            return "Je n'ai pas obtenu de résultat exploitable."
        last = results[-1]
        result = last["result"]
        if not result.get("ok"):
            return self._tool_error_text([last])
        name = last["name"]
        data = result.get("data") or {}
        if name == "list_strategies":
            return formatting.format_strategies(data)
        if name == "run_backtest":
            text = formatting.format_run_summary(data)
            if data.get("run_id"):
                text += (
                    f"\n\nDétail : quant-agent show {data['run_id']}"
                    "\nComparaison : quant-agent compare <run_a> <run_b>"
                )
            return text
        if name == "get_backtest":
            return formatting.format_backtest_detail(data)
        if name == "get_paper_portfolio":
            return formatting.format_portfolio(data)
        if name == "get_risk_report":
            return formatting.format_risk(data)
        if name == "explain_metric":
            return formatting.format_metric(data)
        if name == "get_usage":
            return formatting.format_usage(data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    @staticmethod
    def _estimate_usage(
        messages: list[dict[str, Any]], answer: str
    ) -> dict[str, Any]:
        prompt = "".join(str(message.get("content") or "") for message in messages)
        total = max(1, (len(prompt) + len(answer)) // 4)
        return {"total_tokens": total, "prompt_tokens": total, "completion_tokens": 0}


# ------------------------------------------------------------------ openai-like
class OpenAICompatibleProvider(Provider):
    """Client minimal ``/chat/completions`` avec tool calling."""

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        fallback_models: list[str] | None = None,
        timeout: float = 60.0,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        app_name: str = "Meridian Research",
    ):
        self.base_url = base_url.rstrip("/")
        self.models = [model] + [
            item.strip()
            for item in (fallback_models or [])
            if item.strip() and item.strip() != model
        ]
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.app_name = app_name

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        last_error: ProviderError | None = None
        for model in self.models:
            try:
                return self._request(model, messages, tools)
            except ProviderError as exc:
                last_error = exc
                if exc.status in {401, 403}:
                    raise
                continue
        if last_error is not None:
            raise last_error
        raise ProviderError("aucun modèle configuré.")

    def _request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai(messages),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "meridian-research/0.1",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if "openrouter.ai" in self.base_url:
            headers["HTTP-Referer"] = "https://github.com/adamsrolanda7-cpu/meridian"
            headers["X-Title"] = self.app_name
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            retryable = exc.code == 429 or exc.code >= 500
            raise ProviderError(
                f"HTTP {exc.code} du provider {self.base_url}"
                + (f" : {detail}" if detail else ""),
                retryable=retryable,
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"provider injoignable ({self.base_url}) : {exc.reason}",
                retryable=True,
            ) from exc
        except TimeoutError as exc:
            raise ProviderError(
                f"timeout du provider après {self.timeout:.0f} s.",
                retryable=True,
            ) from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"réponse non JSON du provider : {raw[:200]}"
            ) from exc
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ProviderError(f"erreur provider : {message}")

        try:
            choices = payload["choices"]
            message = choices[0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"réponse provider inattendue : {raw[:300]}"
            ) from exc

        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict)
            )
        calls: list[ToolCall] = []
        for index, tool_call in enumerate(message.get("tool_calls") or []):
            function = tool_call.get("function") or {}
            name = function.get("name")
            if not name:
                continue
            raw_args = function.get("arguments") or "{}"
            if isinstance(raw_args, dict):
                arguments = raw_args
            else:
                try:
                    arguments = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        f"arguments JSON invalides pour {name} : {raw_args[:200]}"
                    ) from exc
            calls.append(
                ToolCall(
                    id=str(tool_call.get("id") or f"call_{index}"),
                    name=str(name),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        usage = payload.get("usage") or {}
        return ProviderResponse(
            text=str(content).strip() if content else None,
            tool_calls=calls,
            usage=usage if isinstance(usage, dict) else {},
            model=model,
        )

    @staticmethod
    def _to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "assistant" and message.get("tool_calls"):
                out.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": json.dumps(
                                        call.get("arguments") or {},
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                            for call in message["tool_calls"]
                        ],
                    }
                )
            elif role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.get("tool_call_id"),
                        "content": message.get("content") or "",
                    }
                )
            else:
                out.append(
                    {
                        "role": role,
                        "content": message.get("content") or "",
                    }
                )
        return out


# ------------------------------------------------------------------------ factory
_ALIASES = {"openrouter", "groq", "ollama", "openai"}


def make_provider(name: str | None = None, model: str | None = None) -> Provider:
    """Construit le provider demandé, avec défauts sûrs."""
    key = (name or os.environ.get("QUANT_AGENT_PROVIDER") or "mock").strip().lower()
    if key == "mock":
        return MockProvider()
    if key not in _ALIASES:
        raise ProviderError(
            f"provider inconnu « {key} » ; utilise mock ou openai "
            "(alias : openrouter, groq, ollama)."
        )

    base_url = os.environ.get("QUANT_AGENT_BASE_URL", "").strip()
    if not base_url:
        base_url = {
            "openrouter": "https://openrouter.ai/api/v1",
            "groq": "https://api.groq.com/openai/v1",
            "ollama": "http://localhost:11434/v1",
            "openai": "https://api.openai.com/v1",
        }[key]

    api_key = os.environ.get("QUANT_AGENT_API_KEY", "").strip()
    if not api_key:
        api_key = {
            "openrouter": os.environ.get("OPENROUTER_API_KEY", ""),
            "groq": os.environ.get("GROQ_API_KEY", ""),
            "openai": os.environ.get("OPENAI_API_KEY", ""),
            "ollama": "",
        }.get(key, "")
        api_key = api_key.strip()

    is_local = any(
        host in base_url for host in ("localhost", "127.0.0.1", "0.0.0.0")
    )
    if not api_key and not is_local:
        raise ProviderError(
            "clé API absente. Définis QUANT_AGENT_API_KEY (ou "
            "OPENROUTER_API_KEY / GROQ_API_KEY / OPENAI_API_KEY), "
            "ou utilise le provider mock (défaut)."
        )

    chosen_model = (model or os.environ.get("QUANT_AGENT_MODEL") or "").strip()
    if not chosen_model:
        raise ProviderError(
            "modèle absent. Définis QUANT_AGENT_MODEL, par exemple :\n"
            "  OpenRouter : export QUANT_AGENT_PROVIDER=openrouter "
            "QUANT_AGENT_MODEL=<modele-gratuit>\n"
            "  Groq       : export QUANT_AGENT_PROVIDER=groq "
            "QUANT_AGENT_MODEL=llama-3.3-70b-versatile\n"
            "  Ollama     : export QUANT_AGENT_PROVIDER=ollama "
            "QUANT_AGENT_MODEL=qwen2.5:7b\n"
            "Le provider mock reste le défaut sans clé."
        )

    fallbacks = [
        item.strip()
        for item in os.environ.get("QUANT_AGENT_FALLBACK_MODELS", "").split(",")
        if item.strip()
    ]
    try:
        timeout = float(os.environ.get("QUANT_AGENT_TIMEOUT", "60"))
    except ValueError:
        timeout = 60.0
    try:
        max_tokens = int(os.environ.get("QUANT_AGENT_MAX_TOKENS", "1500"))
    except ValueError:
        max_tokens = 1500
    try:
        temperature = float(os.environ.get("QUANT_AGENT_TEMPERATURE", "0.2"))
    except ValueError:
        temperature = 0.2

    return OpenAICompatibleProvider(
        base_url=base_url,
        model=chosen_model,
        api_key=api_key or None,
        fallback_models=fallbacks,
        timeout=max(5.0, timeout),
        max_tokens=max(128, max_tokens),
        temperature=min(1.5, max(0.0, temperature)),
    )
