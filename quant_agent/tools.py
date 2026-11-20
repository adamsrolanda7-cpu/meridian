"""Outils déterministes exposés à l'agent.

Chaque outil valide ses arguments, renvoie un JSON stable et ne fait jamais
d'ordre réel. Le Risk Manager n'est pas un outil LLM : le rapport de risque
vient du moteur Meridian.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import unicodedata
from typing import TYPE_CHECKING, Any, Callable

from meridian.config import ROOT

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

from .data_service import DataService, DataServiceError
from .jsonutil import stable_hash, to_jsonable
from .presets import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_STRATEGY,
    build_config,
    get_strategy,
    list_strategies,
)
from .quotas import QuotaManager
from .store import (
    RunStore,
    compare_payloads,
    compact_run,
    compute_run_id,
    summary_from_payload,
    utc_now,
)


class ToolError(RuntimeError):
    """Erreur métier destinée à l'agent, pas une trace Python."""


# --------------------------------------------------------------------- schemas
TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "list_strategies",
        "description": (
            "Liste les stratégies disponibles, leur description et la stratégie "
            "par défaut. Aucun argument."
        ),
        "parameters": {"type": "object", "properties": {}},
        "handler": "_list_strategies",
    },
    {
        "name": "run_backtest",
        "description": (
            "Lance un backtest déterministe sur les données en cache. Retourne "
            "un run_id, les métriques et les avertissements. Le cache est "
            "réutilisé si les mêmes données et paramètres ont déjà été testés."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "enum": ["trend_momentum", "mean_reversion"],
                    "default": DEFAULT_STRATEGY,
                    "description": "Preset de stratégie à tester.",
                },
                "start": {
                    "type": "string",
                    "description": "Date de début ISO, ex. 2024-01-01. Optionnel.",
                },
                "end": {
                    "type": "string",
                    "description": "Date de fin ISO. Par défaut, la dernière bougie.",
                },
                "lookback_days": {
                    "type": "integer",
                    "minimum": 30,
                    "maximum": 3650,
                    "default": DEFAULT_LOOKBACK_DAYS,
                    "description": (
                        "Fenêtre en jours si start est absent. 730 = environ 2 ans."
                    ),
                },
                "initial_equity": {
                    "type": "number",
                    "minimum": 1000,
                    "description": "Capital initial simulé. Défaut : config Meridian.",
                },
                "cost_multiplier": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 10,
                    "default": 1.0,
                    "description": "Multiplicateur des frais et du slippage.",
                },
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Télécharge les données Binance avant le backtest. "
                        "Désactivé par défaut pour rester hors ligne."
                    ),
                },
            },
        },
        "handler": "_run_backtest",
    },
    {
        "name": "get_backtest",
        "description": (
            "Retourne le détail d'un backtest déjà exécuté : métriques, "
            "rendements mensuels, derniers trades, diagnostics."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "default": "latest",
                    "description": "Identifiant bt_... ou latest.",
                },
                "include_curve": {
                    "type": "boolean",
                    "default": False,
                    "description": "Inclure la courbe d'equity complète.",
                },
            },
        },
        "handler": "_get_backtest",
    },
    {
        "name": "compare_backtests",
        "description": (
            "Compare deux backtests : métriques côte à côte, deltas, "
            "corrélation des rendements. Ne recommande pas une stratégie."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id_a": {
                    "type": "string",
                    "description": "Premier run_id bt_...",
                },
                "run_id_b": {
                    "type": "string",
                    "description": "Second run_id bt_...",
                },
            },
            "required": ["run_id_a", "run_id_b"],
        },
        "handler": "_compare_backtests",
    },
    {
        "name": "get_paper_portfolio",
        "description": (
            "Lit le portefeuille paper local : equity, cash, positions, "
            "intentions, régime et dernière séance. Lecture seule."
        ),
        "parameters": {"type": "object", "properties": {}},
        "handler": "_get_paper_portfolio",
    },
    {
        "name": "get_risk_report",
        "description": (
            "Produit le rapport de risque Meridian : fraîcheur des données, "
            "drawdown, Sharpe glissant, exposition, turnover, kill switch. "
            "Le Risk Manager peut mettre un veto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Télécharger les données avant le contrôle.",
                }
            },
        },
        "handler": "_get_risk_report",
    },
    {
        "name": "explain_metric",
        "description": (
            "Explique une métrique de performance ou de risque : définition, "
            "lecture, limite. Aucun chiffre de backtest n'est inventé."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "sharpe, sortino, cagr, drawdown, etc.",
                }
            },
            "required": ["metric"],
        },
        "handler": "_explain_metric",
    },
    {
        "name": "get_usage",
        "description": (
            "Affiche les quotas locaux du jour : messages, backtests, tokens. "
            "Aucun argument."
        ),
        "parameters": {"type": "object", "properties": {}},
        "handler": "_get_usage",
    },
]


# ------------------------------------------------------------------- métriques
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "max_drawdown": (
        "max drawdown",
        "maximum drawdown",
        "drawdown maximal",
        "drawdown max",
        "max dd",
        "dd max",
        "perte maximale",
    ),
    "drawdown": ("drawdown", "sous l eau", "underwater"),
    "sharpe": ("sharpe",),
    "sortino": ("sortino",),
    "calmar": ("calmar",),
    "cagr": ("cagr", "rendement annualise", "croissance annuelle"),
    "volatility": ("volatilite", "volatility", "volatilite annualisee"),
    "turnover_x": ("turnover", "rotation", "turnover annuel"),
    "hit_rate": ("hit rate", "taux de reussite", "win rate"),
    "profit_factor": ("profit factor", "facteur de profit"),
    "expectancy": ("expectancy", "esperance", "pnl moyen"),
    "exposure": ("exposition", "exposure", "gross exposure"),
    "beta": ("beta",),
    "correlation": ("correlation",),
}

METRIC_EXPLANATIONS: dict[str, dict[str, str]] = {
    "cagr": {
        "label": "CAGR",
        "definition": (
            "Taux de croissance annuel composé : le rendement annuel moyen "
            "qu'il faudrait pour passer de l'equity initiale à l'equity finale."
        ),
        "how_to_read": (
            "Un CAGR de 16 % signifie que le backtest composait à 16 % par an "
            "sur la période, pas que chaque année a fait 16 %."
        ),
        "caveat": (
            "Très sensible à la période choisie et aux points de départ/arrivée. "
            "À lire avec le drawdown et le nombre de trades."
        ),
    },
    "sharpe": {
        "label": "Sharpe",
        "definition": (
            "Rendement moyen par unité de volatilité, annualisé. Ici, le taux "
            "sans risque est implicitement zéro."
        ),
        "how_to_read": (
            "Plus haut est mieux, mais un Sharpe élevé avec peu de trades ou une "
            "courte historique n'est pas une preuve. Un Sharpe autour de 1 est "
            "correct pour une stratégie systématique."
        ),
        "caveat": (
            "Le Sharpe pénalise la volatilité à la hausse et suppose des "
            "rendements à peu près normaux. Il ne dit rien du risque de ruine."
        ),
    },
    "sortino": {
        "label": "Sortino",
        "definition": (
            "Comme le Sharpe, mais la volatilité des rendements négatifs est "
            "utilisée à la place de la volatilité totale."
        ),
        "how_to_read": (
            "Utile quand la distribution est asymétrique. Un Sortino très "
            "supérieur au Sharpe indique que la volatilité à la hausse domine."
        ),
        "caveat": "Reste une mesure de performance passée, pas une garantie.",
    },
    "calmar": {
        "label": "Calmar",
        "definition": "CAGR divisé par la valeur absolue du max drawdown.",
        "how_to_read": (
            "Mesure le rendement par unité de pire perte. Plus haut est mieux."
        ),
        "caveat": (
            "Un seul épisode de drawdown peut dominer le ratio. Vérifier la "
            "durée et la récupération."
        ),
    },
    "volatility": {
        "label": "Volatilité",
        "definition": (
            "Écart-type annualisé des rendements quotidiens de la stratégie."
        ),
        "how_to_read": (
            "Une volatilité de 16 % est plus faible qu'un actif crypto isolé, "
            "grâce au cash et au dimensionnement."
        ),
        "caveat": (
            "La volatilité réalisée peut sous-estimer les ruptures de régime."
        ),
    },
    "max_drawdown": {
        "label": "Max drawdown",
        "definition": (
            "Pire baisse depuis un sommet de l'equity jusqu'au creux suivant."
        ),
        "how_to_read": (
            "Un max drawdown de -15 % signifie que le portefeuille a perdu 15 % "
            "depuis son plus haut avant de se rétablir."
        ),
        "caveat": (
            "C'est un extremum historique : un drawdown futur peut être plus "
            "profond. Le temps sous l'eau compte aussi."
        ),
    },
    "drawdown": {
        "label": "Drawdown",
        "definition": "Baisse actuelle depuis le dernier plus haut de l'equity.",
        "how_to_read": (
            "0 % signifie que l'equity est à son plus haut. -8 % signifie "
            "qu'elle est 8 % sous son plus haut."
        ),
        "caveat": "Le drawdown courant peut être temporaire ou annoncer une crise.",
    },
    "turnover_x": {
        "label": "Turnover",
        "definition": (
            "Notionnel total tradé divisé par l'equity moyenne, annualisé."
        ),
        "how_to_read": (
            "Un turnover de 3,6x signifie que le portefeuille trade environ "
            "3,6 fois son equity par an."
        ),
        "caveat": (
            "Un turnover élevé augmente les frais, le slippage et l'impact. "
            "Toujours le lire avec les coûts totaux."
        ),
    },
    "hit_rate": {
        "label": "Hit rate",
        "definition": (
            "Part des trades clôturés avec un PnL positif."
        ),
        "how_to_read": (
            "Un hit rate de 60 % ne suffit pas : il faut comparer la taille des "
            "gains et des pertes. Un système peut être rentable avec 40 %."
        ),
        "caveat": "Ne dit rien de l'expectancy ni de la queue de pertes.",
    },
    "profit_factor": {
        "label": "Profit factor",
        "definition": "Somme des gains divisée par la valeur absolue des pertes.",
        "how_to_read": (
            "Au-dessus de 1, la stratégie gagne plus qu'elle ne perd sur les "
            "trades clôturés. 2,7 est élevé et doit être challengé."
        ),
        "caveat": (
            "Très sensible aux outliers et au nombre de trades. Vérifier la "
            "distribution des PnL."
        ),
    },
    "expectancy": {
        "label": "Expectancy",
        "definition": "PnL moyen par trade clôturé.",
        "how_to_read": (
            "Positive = espérance favorable sur l'échantillon. À comparer aux "
            "frais et au nombre de trades."
        ),
        "caveat": "Un petit échantillon peut produire une expectancy trompeuse.",
    },
    "exposure": {
        "label": "Exposition",
        "definition": "Part moyenne de l'equity investie en positions longues.",
        "how_to_read": (
            "Une exposition moyenne de 14 % signifie que la stratégie est "
            "souvent en cash, ce qui réduit la volatilité et le rendement."
        ),
        "caveat": "L'exposition moyenne masque les pics d'exposition.",
    },
    "beta": {
        "label": "Beta",
        "definition": "Sensibilité des rendements de la stratégie à ceux du BTC.",
        "how_to_read": (
            "Beta proche de 0 = stratégie peu corrélée au BTC. Beta > 1 = "
            "amplifie les mouvements du BTC."
        ),
        "caveat": "Le beta varie selon la période et le régime de marché.",
    },
    "correlation": {
        "label": "Corrélation",
        "definition": "Corrélation des rendements de la stratégie avec le BTC.",
        "how_to_read": (
            "Proche de 1 = suit le BTC. Proche de 0 = diversification. Proche "
            "de -1 = inverse (impossible en long-only spot)."
        ),
        "caveat": "La corrélation n'est pas stable dans le temps.",
    },
}


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().replace("'", " ").replace("’", " ").split())


def _clean_run_id(value: Any) -> str:
    return str(value or "").strip().strip("`").strip()


# ------------------------------------------------------------------- validation
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def validate_arguments(spec: dict[str, Any], args: dict[str, Any]) -> None:
    if not isinstance(args, dict):
        raise ToolError("les arguments doivent être un objet JSON.")
    schema = spec.get("parameters") or {}
    properties = schema.get("properties") or {}
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ToolError(
            f"argument(s) inconnu(s) pour {spec['name']} : {', '.join(unknown)}"
        )
    for key in schema.get("required", []):
        if key not in args or args[key] is None:
            raise ToolError(f"argument requis manquant : {key}")
    for key, value in args.items():
        rule = properties.get(key) or {}
        expected = rule.get("type")
        if value is None or expected is None:
            continue
        if expected == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolError(f"{key} doit être un nombre.")
        elif expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"{key} doit être un entier.")
        elif expected in _TYPE_MAP:
            if not isinstance(value, _TYPE_MAP[expected]):
                raise ToolError(f"{key} doit être de type {expected}.")
        if "enum" in rule and value not in rule["enum"]:
            raise ToolError(
                f"{key} doit être parmi : {', '.join(map(str, rule['enum']))}."
            )
        if expected in {"number", "integer"}:
            minimum = rule.get("minimum")
            maximum = rule.get("maximum")
            if minimum is not None and float(value) < float(minimum):
                raise ToolError(f"{key} doit être >= {minimum}.")
            if maximum is not None and float(value) > float(maximum):
                raise ToolError(f"{key} doit être <= {maximum}.")


# ---------------------------------------------------------------------- tools
class QuantTools:
    """Implémentation des outils, indépendante du provider LLM."""

    def __init__(
        self,
        root: str | None = None,
        quotas: QuotaManager | None = None,
        allow_synthetic: bool = False,
    ):
        self.root = root or ROOT
        self.quotas = quotas or QuotaManager(root=self.root)
        self.data = DataService(allow_synthetic=allow_synthetic)
        self.store = RunStore(root=self.root)
        self._dispatch: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            definition["name"]: getattr(self, definition["handler"])
            for definition in TOOL_DEFS
        }

    # ------------------------------------------------------------------ public
    def call(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        started = time.perf_counter()
        definition = next(
            (item for item in TOOL_DEFS if item["name"] == name), None
        )
        if definition is None:
            return {
                "ok": False,
                "tool": name,
                "error": f"outil inconnu : {name}",
                "duration_ms": 0,
            }
        try:
            validate_arguments(definition, args or {})
            data = self._dispatch[name](args or {})
            return {
                "ok": True,
                "tool": name,
                "data": to_jsonable(data),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except ToolError as exc:
            return {
                "ok": False,
                "tool": name,
                "error": str(exc),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except (DataServiceError, KeyError, ValueError) as exc:
            return {
                "ok": False,
                "tool": name,
                "error": str(exc),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:  # garde-fou : l'agent reçoit une erreur lisible
            return {
                "ok": False,
                "tool": name,
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }

    # ----------------------------------------------------------------- helpers
    def _parse_ts(
        self, value: str | None, default: pd.Timestamp | None = None
    ) -> pd.Timestamp:
        import pandas as pd

        if value:
            try:
                ts = pd.Timestamp(value)
            except Exception as exc:
                raise ToolError(
                    f"date invalide « {value} » ; format attendu YYYY-MM-DD."
                ) from exc
        elif default is not None:
            ts = default
        else:
            raise ToolError("date manquante.")
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts

    def _load_run(self, run_id: str) -> dict[str, Any]:
        cleaned = _clean_run_id(run_id)
        payload = (
            self.store.latest()
            if cleaned.lower() in {"", "latest", "dernier", "derniere", "last"}
            else self.store.load(cleaned)
        )
        if payload is None:
            raise ToolError(
                f"backtest introuvable : {run_id}. "
                "Utilise get_backtest avec un run_id bt_... ou 'latest'."
            )
        return payload

    @staticmethod
    def _trade(trade: dict[str, Any]) -> dict[str, Any]:
        item = dict(trade)
        reason = str(item.get("reason") or "")
        item["reason"] = reason[:180]
        return to_jsonable(item)

    # ------------------------------------------------------------------ tools
    def _list_strategies(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategies": list_strategies(),
            "default": DEFAULT_STRATEGY,
            "count": len(list_strategies()),
        }

    def _run_backtest(self, args: dict[str, Any]) -> dict[str, Any]:
        import pandas as pd

        from meridian.backtest import Backtest
        from meridian.features import FeatureStore
        from meridian.metrics import monthly_returns

        strategy_name = str(args.get("strategy") or DEFAULT_STRATEGY)
        try:
            preset = get_strategy(strategy_name)
            cfg = build_config(strategy_name)
        except KeyError as exc:
            raise ToolError(str(exc)) from exc

        panel, source = self.data.load(refresh=bool(args.get("refresh", False)))
        last_bar = self.data.last_bar(panel)
        first_bar = self.data.first_bar(panel)
        warnings: list[str] = []

        end_ts = self._parse_ts(args.get("end"), default=last_bar)
        if end_ts > last_bar:
            warnings.append(
                f"fin demandée après la dernière bougie ({last_bar.date()}) ; "
                "fin ramenée à la dernière bougie."
            )
            end_ts = last_bar
        if args.get("start"):
            start_ts = self._parse_ts(str(args["start"]))
        else:
            try:
                lookback = int(args.get("lookback_days") or DEFAULT_LOOKBACK_DAYS)
            except (TypeError, ValueError) as exc:
                raise ToolError("lookback_days doit être un entier.") from exc
            start_ts = end_ts - pd.Timedelta(days=lookback)
        if start_ts < first_bar:
            warnings.append(
                f"début demandé avant la première bougie ({first_bar.date()}) ; "
                "début ramené à la première bougie."
            )
            start_ts = first_bar
        if start_ts >= end_ts:
            raise ToolError(
                f"fenêtre vide : début {start_ts.date()} >= fin {end_ts.date()}."
            )

        raw_equity = args.get("initial_equity")
        initial_equity = float(
            raw_equity
            if raw_equity is not None
            else cfg.get("initial_equity", 100_000.0)
        )
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            raise ToolError("initial_equity doit être strictement positive.")
        raw_cost = args.get("cost_multiplier")
        cost_multiplier = float(1.0 if raw_cost is None else raw_cost)
        if not math.isfinite(cost_multiplier) or cost_multiplier < 0:
            raise ToolError("cost_multiplier doit être >= 0.")

        snapshot = self.data.snapshot(panel)
        start_text = start_ts.date().isoformat()
        end_text = end_ts.date().isoformat()
        run_id = compute_run_id(
            strategy=strategy_name,
            cfg=cfg,
            start=start_text,
            end=end_text,
            initial_equity=initial_equity,
            cost_multiplier=cost_multiplier,
            data_snapshot=snapshot,
        )

        cached = self.store.load(run_id)
        if cached is not None:
            summary = summary_from_payload(cached)
            summary["cached"] = True
            return summary

        self.quotas.check("backtests")
        try:
            quality = self.data.quality(panel)
            feature_store = FeatureStore.build(panel, cfg)
            result = Backtest(feature_store, cfg).run(
                start=start_text,
                end=end_text,
                initial_equity=initial_equity,
                cost_multiplier=cost_multiplier,
            )
        except Exception as exc:
            raise ToolError(f"backtest impossible : {exc}") from exc

        stale_days = self.data.stale_days(panel)
        if stale_days > 3:
            warnings.append(
                f"données en cache datées du {last_bar.date()} "
                f"(retard {stale_days} j) ; relance avec refresh=true pour "
                "télécharger les bougies manquantes."
            )
        bad_assets = [
            symbol
            for symbol, row in quality.items()
            if row.get("status") != "OK"
        ]
        if bad_assets:
            warnings.append(
                "qualité à vérifier : " + ", ".join(bad_assets) + "."
            )
        if not result.metrics.get("fills"):
            warnings.append(
                "aucun fill sur la période : la stratégie est restée en cash "
                "ou les seuils n'ont pas été atteints."
            )
        if int(result.metrics.get("days") or 0) < 60:
            warnings.append("historique court : les métriques sont fragiles.")

        curve = [
            {"date": index.isoformat(), "equity": float(value)}
            for index, value in result.equity.items()
        ]
        monthly = []
        try:
            frame = monthly_returns(result.equity)
            for index, row in frame.iterrows():
                monthly.append(
                    {
                        "month": index.strftime("%Y-%m"),
                        "return": float(row["return"]),
                    }
                )
        except Exception:
            monthly = []

        diagnostics = result.diagnostics or {}
        governor = diagnostics.get("governor") or {}
        compact_diagnostics = {
            "governor": governor,
            "halt_dates": diagnostics.get("halt_dates", []),
            "realized_pnl": diagnostics.get("realized_pnl"),
            "unrealized_pnl": diagnostics.get("unrealized_pnl"),
            "final_cash": diagnostics.get("final_cash"),
            "fees_paid": diagnostics.get("fees_paid"),
            "last_prices": diagnostics.get("last_prices", {}),
        }

        payload = {
            "run_id": run_id,
            "created_at": utc_now(),
            "engine": "meridian",
            "strategy": strategy_name,
            "label": preset["label"],
            "description": preset["description"],
            "overrides": preset["overrides"],
            "config_hash": stable_hash(cfg, 16),
            "start": start_text,
            "end": end_text,
            "initial_equity": initial_equity,
            "cost_multiplier": cost_multiplier,
            "data": {
                "source": source,
                "last_bar": str(last_bar.date()),
                "stale_days": stale_days,
                "snapshot": snapshot,
            },
            "quality": {
                "assets": len(quality),
                "assets_bad": bad_assets,
                "last_bar": str(last_bar.date()),
            },
            "metrics": result.metrics,
            "equity_curve": curve,
            "monthly_returns": monthly,
            "recent_trades": [self._trade(trade) for trade in result.trades[-50:]],
            "diagnostics": compact_diagnostics,
            "warnings": warnings,
        }
        self.store.save(payload)
        self.quotas.consume("backtests")
        summary = summary_from_payload(payload)
        summary["cached"] = False
        return summary

    def _get_backtest(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = self._load_run(str(args.get("run_id") or "latest"))
        return compact_run(payload, include_curve=bool(args.get("include_curve", False)))

    def _compare_backtests(self, args: dict[str, Any]) -> dict[str, Any]:
        left = self._load_run(str(args.get("run_id_a") or ""))
        right = self._load_run(str(args.get("run_id_b") or ""))
        return compare_payloads(left, right)

    def _get_paper_portfolio(self, args: dict[str, Any]) -> dict[str, Any]:
        from meridian.config import load_config

        state_path = os.path.join(self.root, "runs", "paper", "state.json")
        if not os.path.exists(state_path):
            return {
                "initialized": False,
                "hint": "python3 -m meridian paper --init",
            }
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(f"état paper illisible : {exc}") from exc

        cfg = load_config()
        equity = float(state.get("equity") or state.get("initial_equity") or 0.0)
        initial_equity = float(state.get("initial_equity") or equity)
        raw_positions = state.get("positions") or {}
        positions = []
        market_value_total = 0.0
        if isinstance(raw_positions, dict):
            for asset, raw in sorted(raw_positions.items()):
                if not isinstance(raw, dict):
                    continue
                qty = float(raw.get("qty", 0.0) or 0.0)
                avg_price = float(raw.get("avg_price", 0.0) or 0.0)
                last_price = float(raw.get("last_price", avg_price) or avg_price)
                stop = float(raw.get("stop", 0.0) or 0.0)
                market_value = qty * last_price
                market_value_total += market_value
                positions.append(
                    {
                        "asset": asset,
                        "qty": qty,
                        "avg_price": avg_price,
                        "last_price": last_price,
                        "stop": stop,
                        "sleeve": raw.get("sleeve"),
                        "market_value": market_value,
                        "weight": (market_value / equity) if equity else 0.0,
                        "unrealized_pnl": (last_price - avg_price) * qty,
                    }
                )
        exposure = (market_value_total / equity) if equity else 0.0
        memo_excerpt = ""
        memo_path = os.path.join(self.root, "runs", "paper", "memo.md")
        if os.path.exists(memo_path):
            try:
                with open(memo_path, "r", encoding="utf-8") as fh:
                    lines = [
                        line.rstrip()
                        for line in fh.read().splitlines()
                        if line.strip()
                    ]
                memo_excerpt = "\n".join(lines[:7])
            except OSError:
                memo_excerpt = ""
        runs = state.get("runs") or []
        last_run = runs[-1] if isinstance(runs, list) and runs else None
        expected_hash = hashlib.sha256(
            json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        cash = float(state.get("cash") or 0.0)
        return {
            "initialized": True,
            "start": state.get("start"),
            "last_bar": state.get("last_bar"),
            "updated_at": state.get("updated_at"),
            "initial_equity": initial_equity,
            "equity": equity,
            "cash": cash,
            "cash_pct": (cash / equity) if equity else 0.0,
            "exposure": exposure,
            "drawdown": float(state.get("drawdown") or 0.0),
            "regime": state.get("last_regime"),
            "gross_target": float(state.get("gross_target") or 0.0),
            "data_source": state.get("data_source"),
            "config_match": state.get("config_hash") == expected_hash,
            "runs": len(runs) if isinstance(runs, list) else 0,
            "positions": positions,
            "pending_orders": state.get("pending_orders") or [],
            "last_run": last_run,
            "memo_excerpt": memo_excerpt,
        }

    def _get_risk_report(self, args: dict[str, Any]) -> dict[str, Any]:
        from meridian import monitor as monitor_mod
        from meridian.config import load_config

        cfg = load_config()
        try:
            payload = monitor_mod.evaluate(
                cfg, refresh=bool(args.get("refresh", False))
            )
        except Exception as exc:
            raise ToolError(f"rapport de risque impossible : {exc}") from exc
        # monitor.evaluate expose ses informations au niveau racine.
        raw_data = payload.get("data") or payload
        quality = raw_data.get("quality") or {}
        bad = [
            symbol
            for symbol, row in quality.items()
            if row.get("status") != "OK"
        ]
        data_info = {
            "source": raw_data.get("source"),
            "last_bar": raw_data.get("last_bar"),
            "stale_days": raw_data.get("stale_days"),
            "assets": len(quality),
            "assets_ok": len(quality) - len(bad),
            "assets_bad": bad,
        }
        return {
            "status": payload.get("status"),
            "kill_switch": bool(payload.get("kill_switch")),
            "generated_at": payload.get("generated_at"),
            "config_hash": payload.get("config_hash"),
            "data": data_info,
            "paper": payload.get("paper"),
            "rolling": payload.get("rolling"),
            "checks": payload.get("checks", []),
            "replay": payload.get("replay"),
            "attribution": payload.get("attribution"),
        }

    def _explain_metric(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = str(args.get("metric") or "").strip()
        normalized = _norm(raw)
        found: str | None = None
        for key, aliases in METRIC_ALIASES.items():
            if normalized == _norm(key) or any(
                _norm(alias) in normalized for alias in aliases
            ):
                found = key
                break
        available = list(METRIC_EXPLANATIONS)
        if found is None:
            return {
                "found": False,
                "metric": raw,
                "available": available,
            }
        explanation = dict(METRIC_EXPLANATIONS[found])
        explanation.update({"found": True, "metric": found})
        return explanation

    def _get_usage(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.quotas.snapshot()


# --------------------------------------------------------------------- registry
class ToolRegistry:
    """Façade utilisée par l'agent : schémas + appel validé."""

    def __init__(self, tools: QuantTools):
        self.tools = tools
        self._defs = {item["name"]: item for item in TOOL_DEFS}

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["parameters"],
                },
            }
            for item in TOOL_DEFS
        ]

    def call(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        definition = self._defs.get(name)
        if definition is None:
            return {
                "ok": False,
                "tool": name,
                "error": f"outil inconnu : {name}",
                "duration_ms": 0,
            }
        return self.tools.call(name, args or {})

    def names(self) -> list[str]:
        return list(self._defs)


def tool_catalog() -> list[dict[str, Any]]:
    """Catalogue lisible pour la commande ``tools``."""
    return [
        {
            "name": item["name"],
            "description": item["description"],
            "parameters": item["parameters"],
        }
        for item in TOOL_DEFS
    ]
