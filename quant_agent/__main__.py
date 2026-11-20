"""CLI ``quant-agent`` — prototype de recherche quant assistée.

Commandes principales :
    quant-agent ask "question"
    quant-agent chat
    quant-agent backtest --strategy trend_momentum
    quant-agent compare <run_a> <run_b>
    quant-agent show <run_id>
    quant-agent strategies
    quant-agent portfolio
    quant-agent risk
    quant-agent usage
    quant-agent tools
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from . import __version__
from .agent import Agent, TurnResult
from .data_service import DataServiceError
from .formatting import (
    format_backtest_detail,
    format_comparison,
    format_portfolio,
    format_risk,
    format_run_summary,
    format_strategies,
    format_usage,
    help_text,
)
from .jsonutil import to_jsonable
from .policy import MODES, Policy
from .presets import STRATEGIES
from .providers import Provider, ProviderError, make_provider
from .quotas import QuotaExceeded, QuotaManager
from .tools import QuantTools, ToolRegistry, tool_catalog


@dataclass
class Runtime:
    quotas: QuotaManager
    tools: QuantTools
    registry: ToolRegistry
    provider: Provider | None = None
    policy: Policy | None = None
    agent: Agent | None = None


def _runtime(args: argparse.Namespace, need_provider: bool = False) -> Runtime:
    quotas = QuotaManager(enabled=not getattr(args, "no_quota", False))
    tools = QuantTools(quotas=quotas)
    registry = ToolRegistry(tools)
    runtime = Runtime(quotas=quotas, tools=tools, registry=registry)
    if need_provider:
        provider = make_provider(
            getattr(args, "provider", None),
            getattr(args, "model", None),
        )
        policy = Policy(getattr(args, "mode", "kraken"))
        runtime.provider = provider
        runtime.policy = policy
        runtime.agent = Agent(
            provider,
            registry,
            policy=policy,
            quotas=quotas,
        )
    return runtime


def _print_json(payload: Any) -> None:
    print(json.dumps(to_jsonable(payload), indent=2, ensure_ascii=False))


def _tool_data(result: dict[str, Any]) -> dict[str, Any] | None:
    if not result.get("ok"):
        print(f"Erreur : {result.get('error') or 'erreur inconnue'}", file=sys.stderr)
        return None
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _print_traces(traces: list[Any]) -> None:
    for trace in traces:
        status = "ok" if trace.ok else "ERREUR"
        args = json.dumps(trace.arguments, ensure_ascii=False)
        print(
            f"[tool] {trace.name} {args} · {status} · {trace.duration_ms} ms",
            file=sys.stderr,
        )
        if trace.error:
            print(f"       {trace.error}", file=sys.stderr)
        if trace.run_ids:
            print(f"       run_id: {', '.join(trace.run_ids)}", file=sys.stderr)


def _provider_label(provider: Provider) -> str:
    models = getattr(provider, "models", None)
    if models:
        return f"{provider.name}:{models[0]}"
    return provider.name


# --------------------------------------------------------------------- commands
def cmd_strategies(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    data = _tool_data(runtime.registry.call("list_strategies", {}))
    if data is None:
        return 1
    if args.json:
        _print_json(data)
    else:
        print(format_strategies(data))
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    payload: dict[str, Any] = {
        "strategy": args.strategy,
        "lookback_days": args.lookback_days,
        "cost_multiplier": args.cost_multiplier,
        "refresh": args.refresh,
    }
    if args.start:
        payload["start"] = args.start
    if args.end:
        payload["end"] = args.end
    if args.initial_equity is not None:
        payload["initial_equity"] = args.initial_equity
    result = runtime.registry.call("run_backtest", payload)
    data = _tool_data(result)
    if data is None:
        return 1
    if args.json:
        _print_json(data)
        return 0
    if args.detail and data.get("run_id"):
        detail = _tool_data(
            runtime.registry.call(
                "get_backtest",
                {"run_id": data["run_id"], "include_curve": False},
            )
        )
        if detail is not None:
            print(format_backtest_detail(detail))
            return 0
    print(format_run_summary(data))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    data = _tool_data(
        runtime.registry.call(
            "compare_backtests",
            {"run_id_a": args.run_id_a, "run_id_b": args.run_id_b},
        )
    )
    if data is None:
        return 1
    if args.json:
        _print_json(data)
    else:
        print(format_comparison(data))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    data = _tool_data(
        runtime.registry.call(
            "get_backtest",
            {"run_id": args.run_id, "include_curve": args.curve},
        )
    )
    if data is None:
        return 1
    if args.json:
        _print_json(data)
    else:
        print(format_backtest_detail(data))
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    data = _tool_data(runtime.registry.call("get_paper_portfolio", {}))
    if data is None:
        return 1
    if args.json:
        _print_json(data)
    else:
        print(format_portfolio(data))
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    data = _tool_data(
        runtime.registry.call("get_risk_report", {"refresh": args.refresh})
    )
    if data is None:
        return 1
    if args.json:
        _print_json(data)
    else:
        print(format_risk(data))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    if args.reset:
        runtime.quotas.reset()
    snapshot = runtime.quotas.snapshot()
    if args.json:
        _print_json(snapshot)
    else:
        print(format_usage(snapshot))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    catalog = tool_catalog()
    if args.json:
        _print_json({"tools": catalog})
        return 0
    print(f"Outils de l'agent ({len(catalog)}) :")
    for item in catalog:
        print(f"- {item['name']} — {item['description']}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    text = " ".join(args.question).strip()
    if not text:
        print("Pose une question, par exemple : « Fais un backtest trend sur 2 ans ».")
        return 2
    runtime = _runtime(args, need_provider=True)
    assert runtime.agent is not None
    result = runtime.agent.ask(text)
    if args.json:
        _print_json(
            {
                "answer": result.answer,
                "session_id": result.session_id,
                "provider": result.provider,
                "mode": result.mode,
                "run_ids": result.run_ids,
                "tokens": result.tokens,
                "tools": [
                    {
                        "name": trace.name,
                        "arguments": trace.arguments,
                        "ok": trace.ok,
                        "duration_ms": trace.duration_ms,
                        "error": trace.error,
                        "run_ids": trace.run_ids,
                    }
                    for trace in result.traces
                ],
            }
        )
        return 0
    print(result.answer)
    if args.trace:
        _print_traces(result.traces)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    runtime = _runtime(args, need_provider=True)
    assert runtime.agent is not None and runtime.provider is not None
    session_id = uuid.uuid4().hex[:8]
    history: list[dict[str, Any]] = []
    trace = bool(args.trace)
    print(
        f"Meridian Research · provider={_provider_label(runtime.provider)} "
        f"· mode={runtime.policy.name if runtime.policy else 'kraken'} · paper only"
    )
    print("Tape /help pour les commandes, /exit pour quitter.")
    print()
    while True:
        try:
            line = input("vous> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break
        if not line:
            continue
        if line.startswith("/"):
            command, _, argument = line.partition(" ")
            command = command.lower()
            argument = argument.strip()
            if command in {"/exit", "/quit"}:
                break
            if command == "/help":
                print(help_text())
                print()
                continue
            if command == "/clear":
                history.clear()
                print("Historique effacé.")
                print()
                continue
            if command == "/trace":
                if argument.lower() in {"on", "off"}:
                    trace = argument.lower() == "on"
                else:
                    trace = not trace
                print(f"Trace {'activée' if trace else 'désactivée'}.")
                print()
                continue
            if command == "/mode":
                try:
                    runtime.agent.policy = Policy(argument or "kraken")
                    print(f"Mode : {runtime.agent.policy.name}")
                except ValueError as exc:
                    print(f"Erreur : {exc}")
                print()
                continue
            if command == "/reset":
                runtime.quotas.reset()
                print("Quotas locaux réinitialisés.")
                print()
                continue
            if command in {"/strategies", "/portfolio", "/risk", "/usage"}:
                tool_name = {
                    "/strategies": "list_strategies",
                    "/portfolio": "get_paper_portfolio",
                    "/risk": "get_risk_report",
                    "/usage": "get_usage",
                }[command]
                data = _tool_data(runtime.registry.call(tool_name, {}))
                if data is not None:
                    if command == "/strategies":
                        print(format_strategies(data))
                    elif command == "/portfolio":
                        print(format_portfolio(data))
                    elif command == "/risk":
                        print(format_risk(data))
                    else:
                        print(format_usage(data))
                print()
                continue
            print("Commande inconnue. /help pour la liste.")
            print()
            continue

        try:
            result: TurnResult = runtime.agent.ask(
                line, history=history, session_id=session_id
            )
        except QuotaExceeded as exc:
            print(f"Quota atteint : {exc}")
            print()
            continue
        except ProviderError as exc:
            print(f"Provider : {exc}")
            print()
            continue
        print()
        print(result.answer)
        if trace:
            print()
            _print_traces(result.traces)
        print()
        history.append({"role": "user", "content": line})
        history.append({"role": "assistant", "content": result.answer})
        keep = max(2, int(args.max_history)) * 2
        if len(history) > keep:
            history[:] = history[-keep:]
    return 0


# ------------------------------------------------------------------------ parser
def _add_agent_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        default=os.environ.get("QUANT_AGENT_PROVIDER", "mock"),
        help="mock (défaut), openai, openrouter, groq ou ollama.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("QUANT_AGENT_MODEL") or None,
        help="Nom du modèle pour un provider OpenAI-compatible.",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(MODES),
        default=os.environ.get("QUANT_AGENT_MODE", "kraken"),
        help="Budget de raisonnement : fast, kraken (défaut), deep.",
    )
    parser.add_argument(
        "--no-quota",
        action="store_true",
        help="Désactiver les quotas locaux pour cette session.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-agent",
        description=(
            "Meridian Research — recherche, backtest, risque et paper trading. "
            "Paper only, aucun ordre réel."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="Poser une question à l'agent.")
    ask.add_argument("question", nargs="+", help="Question en langage naturel.")
    _add_agent_args(ask)
    ask.add_argument("--trace", action="store_true", help="Afficher les outils appelés.")
    ask.add_argument("--json", action="store_true", help="Sortie JSON.")
    ask.set_defaults(func=cmd_ask)

    chat = sub.add_parser("chat", help="Session interactive avec l'agent.")
    _add_agent_args(chat)
    chat.add_argument("--trace", action="store_true", help="Afficher les outils appelés.")
    chat.add_argument(
        "--max-history",
        type=int,
        default=12,
        help="Nombre de messages d'historique conservés (défaut 12).",
    )
    chat.set_defaults(func=cmd_chat)

    backtest = sub.add_parser("backtest", help="Lancer un backtest déterministe.")
    backtest.add_argument(
        "--strategy",
        choices=sorted(STRATEGIES),
        default="trend_momentum",
        help="Preset de stratégie.",
    )
    backtest.add_argument("--start", help="Date de début YYYY-MM-DD.")
    backtest.add_argument("--end", help="Date de fin YYYY-MM-DD.")
    backtest.add_argument(
        "--lookback-days",
        type=int,
        default=730,
        help="Fenêtre si --start est absent (défaut 730 jours).",
    )
    backtest.add_argument("--initial-equity", type=float, default=None)
    backtest.add_argument("--cost-multiplier", type=float, default=1.0)
    backtest.add_argument(
        "--refresh", action="store_true", help="Télécharger les données avant le test."
    )
    backtest.add_argument("--detail", action="store_true", help="Afficher le détail.")
    backtest.add_argument("--json", action="store_true", help="Sortie JSON.")
    backtest.add_argument(
        "--no-quota", action="store_true", help="Désactiver les quotas locaux."
    )
    backtest.set_defaults(func=cmd_backtest)

    compare = sub.add_parser("compare", help="Comparer deux backtests.")
    compare.add_argument("run_id_a")
    compare.add_argument("run_id_b")
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(func=cmd_compare)

    show = sub.add_parser("show", help="Afficher un backtest.")
    show.add_argument("run_id", nargs="?", default="latest")
    show.add_argument("--curve", action="store_true", help="Inclure la courbe d'equity.")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    strategies = sub.add_parser("strategies", help="Lister les stratégies.")
    strategies.add_argument("--json", action="store_true")
    strategies.set_defaults(func=cmd_strategies)

    portfolio = sub.add_parser("portfolio", help="Lire le portefeuille paper.")
    portfolio.add_argument("--json", action="store_true")
    portfolio.set_defaults(func=cmd_portfolio)

    risk = sub.add_parser("risk", help="Produire le rapport de risque.")
    risk.add_argument(
        "--refresh", action="store_true", help="Télécharger les données avant contrôle."
    )
    risk.add_argument("--json", action="store_true")
    risk.set_defaults(func=cmd_risk)

    usage = sub.add_parser("usage", help="Afficher les quotas locaux.")
    usage.add_argument("--reset", action="store_true", help="Réinitialiser les quotas.")
    usage.add_argument("--json", action="store_true")
    usage.set_defaults(func=cmd_usage)

    tools = sub.add_parser("tools", help="Lister les outils de l'agent.")
    tools.add_argument("--json", action="store_true")
    tools.set_defaults(func=cmd_tools)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except QuotaExceeded as exc:
        print(f"Quota atteint : {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"Provider : {exc}", file=sys.stderr)
        return 2
    except DataServiceError as exc:
        print(f"Données : {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:  # noqa: BLE001 — message CLI, pas de stack trace par défaut
        if os.environ.get("QUANT_AGENT_DEBUG") == "1":
            raise
        print(f"Erreur : {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Astuce : QUANT_AGENT_DEBUG=1 pour la trace complète.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
