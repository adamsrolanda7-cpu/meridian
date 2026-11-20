"""Formatage texte des réponses du CLI et du provider mock.

Tout est en français, avec des nombres lisibles. Les fonctions prennent les
dictionnaires renvoyés par les outils, jamais des valeurs inventées.
"""
from __future__ import annotations

import math
from typing import Any, Iterable


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def pct(value: Any, digits: int = 1, signed: bool = True) -> str:
    number = _float(value)
    if number is None:
        return "n/a"
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number * 100:.{digits}f} %".replace(".", ",")


def num(value: Any, digits: int = 2) -> str:
    number = _float(value)
    if number is None:
        return "n/a"
    return f"{number:.{digits}f}".replace(".", ",")


def money(value: Any, digits: int = 0) -> str:
    number = _float(value)
    if number is None:
        return "n/a"
    text = f"{number:,.{digits}f}"
    return text.replace(",", " ") + " $"


def integer(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "n/a"
    return f"{int(round(number)):,}".replace(",", " ")


def date(value: Any) -> str:
    if value is None:
        return "n/a"
    text = str(value)
    # Les trades Meridian utilisent parfois "YYYY-MM-DD HH:MM:SS+00:00".
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text.split("T", 1)[0]


def bullet(items: Iterable[str]) -> list[str]:
    return [f"- {item}" for item in items if item]


def format_strategies(data: dict[str, Any]) -> str:
    strategies = data.get("strategies") or []
    lines = [f"Stratégies disponibles ({len(strategies)}) :"]
    for item in strategies:
        marker = " · défaut" if item.get("default") else ""
        lines.append(f"- {item.get('name')}{marker} — {item.get('label')}")
        description = item.get("description")
        if description:
            lines.append(f"  {description}")
    if data.get("default"):
        lines.append("")
        lines.append(
            "Le moteur reste long-only spot, sans levier. Les backtests sont "
            "des simulations, pas des ordres."
        )
    return "\n".join(lines)


def format_run_summary(data: dict[str, Any]) -> str:
    metrics = data.get("metrics") or {}
    cached = " · cache" if data.get("cached") else ""
    lines = [
        f"Backtest {data.get('run_id')}{cached}",
        f"Stratégie : {data.get('label') or data.get('strategy')} "
        f"({data.get('strategy')})",
        f"Période   : {date(metrics.get('start') or data.get('start'))} → "
        f"{date(metrics.get('end') or data.get('end'))} "
        f"({integer(metrics.get('days'))} j)",
        f"Equity    : {money(metrics.get('initial_equity') or data.get('initial_equity'))} → "
        f"{money(metrics.get('final_equity'))} "
        f"({pct(metrics.get('total_return'))})",
        f"CAGR {pct(metrics.get('cagr'))} · Vol {pct(metrics.get('volatility'))} · "
        f"Sharpe {num(metrics.get('sharpe'))} · Sortino {num(metrics.get('sortino'))}",
        f"Max DD {pct(metrics.get('max_drawdown'))} · "
        f"Calmar {num(metrics.get('calmar'))} · "
        f"Turnover {num(metrics.get('turnover_x'), 1)}x",
        f"Trades {integer(metrics.get('fills'))} · "
        f"Hit rate {pct(metrics.get('hit_rate'))} · "
        f"Profit factor {num(metrics.get('profit_factor'))}",
        f"Coûts {money(metrics.get('trading_costs'))} · "
        f"Exposition moyenne {pct(metrics.get('avg_gross_exposure'))}",
    ]
    last_bar = data.get("data_last_bar")
    if last_bar:
        lines.append(f"Données : dernière bougie {date(last_bar)}")
    warnings = data.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Attention :")
        lines.extend(bullet(warnings))
    return "\n".join(lines)


def format_backtest_detail(data: dict[str, Any]) -> str:
    lines = [format_run_summary(data)]
    metrics = data.get("metrics") or {}
    benchmark = metrics.get("benchmark")
    if benchmark:
        lines.append("")
        lines.append(
            "Benchmark BTC : "
            f"{pct(benchmark.get('total_return'))} · "
            f"Sharpe {num(benchmark.get('sharpe'))} · "
            f"Max DD {pct(benchmark.get('max_drawdown'))} · "
            f"beta {num(benchmark.get('beta'))}"
        )
    monthly = data.get("monthly_returns") or []
    if monthly:
        lines.append("")
        lines.append("Derniers mois :")
        tail = monthly[-12:]
        chunks = [
            f"{item.get('month')} {pct(item.get('return'))}" for item in tail
        ]
        lines.append(" · ".join(chunks))
    trades = data.get("recent_trades") or []
    if trades:
        lines.append("")
        lines.append("Derniers trades :")
        lines.append("Date        Actif       Sens   Notionnel      PnL")
        for trade in trades[-8:]:
            lines.append(
                f"{date(trade.get('ts')):<11} "
                f"{str(trade.get('asset', '—')):<11} "
                f"{str(trade.get('side', '—')):<6} "
                f"{money(trade.get('notional')):>12} "
                f"{money(trade.get('pnl')):>10}"
            )
    diagnostics = data.get("diagnostics") or {}
    if diagnostics:
        governor = diagnostics.get("governor") or {}
        if governor:
            peak = _float(governor.get("peak"))
            previous = _float(governor.get("prev_equity"))
            drawdown = (
                previous / peak - 1.0
                if peak and previous is not None and peak > 0
                else None
            )
            lines.append("")
            lines.append(
                "Gouverneur : "
                f"drawdown {pct(drawdown)} · "
                f"halt {'actif' if governor.get('halted') else 'inactif'}"
            )
    lines.append("")
    lines.append(
        "Paper only. Les performances passées ne préjugent pas des performances futures."
    )
    return "\n".join(lines)


def _metric_row(
    label: str,
    key: str,
    left: Any,
    right: Any,
    delta: Any,
    kind: str,
) -> str:
    def render(value: Any) -> str:
        if kind == "pct":
            return pct(value)
        if kind == "int":
            return integer(value)
        return num(value)

    sign = ""
    number = _float(delta)
    if kind == "pct" and number is not None:
        sign = "+" if number > 0 else ""
        delta_text = f"{sign}{number * 100:.1f} %".replace(".", ",")
    elif kind == "int" and number is not None:
        delta_text = f"{number:+.0f}"
    else:
        delta_text = f"{number:+.2f}".replace(".", ",") if number is not None else "n/a"
    return f"{label:<16} {render(left):>10} {render(right):>10} {delta_text:>11}"


def format_comparison(data: dict[str, Any]) -> str:
    runs = data.get("runs") or []
    if len(runs) < 2:
        return "Comparaison impossible : il faut deux runs."
    left, right = runs[0], runs[1]
    deltas = data.get("deltas") or {}
    lines = [
        "Comparaison de backtests",
        f"A : {left.get('run_id')} · {left.get('label')}",
        f"B : {right.get('run_id')} · {right.get('label')}",
        "",
        f"{'Métrique':<16} {'A':>10} {'B':>10} {'Δ (B-A)':>11}",
        "-" * 52,
    ]
    rows = (
        ("CAGR", "cagr", "pct"),
        ("Volatilité", "volatility", "pct"),
        ("Sharpe", "sharpe", "num"),
        ("Sortino", "sortino", "num"),
        ("Calmar", "calmar", "num"),
        ("Max DD", "max_drawdown", "pct"),
        ("Turnover", "turnover_x", "num"),
        ("Hit rate", "hit_rate", "pct"),
        ("Profit factor", "profit_factor", "num"),
        ("Trades", "fills", "int"),
    )
    for label, key, kind in rows:
        lines.append(
            _metric_row(
                label,
                key,
                (left.get("metrics") or {}).get(key),
                (right.get("metrics") or {}).get(key),
                deltas.get(key),
                kind,
            )
        )
    correlation = _float(data.get("correlation"))
    lines.append("")
    if correlation is not None:
        lines.append(f"Corrélation des rendements quotidiens : {num(correlation)}")
    better_sharpe = _best_by(runs, "sharpe")
    better_cagr = _best_by(runs, "cagr")
    # Un drawdown moins négatif est un meilleur drawdown : on garde le max.
    better_dd = _best_by(runs, "max_drawdown")
    if better_sharpe:
        lines.append(f"Meilleur Sharpe : {better_sharpe}")
    if better_cagr:
        lines.append(f"Meilleur CAGR : {better_cagr}")
    if better_dd:
        lines.append(f"Drawdown le plus faible : {better_dd}")
    note = data.get("note")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _best_by(
    runs: list[dict[str, Any]], key: str, reverse: bool = False
) -> str | None:
    values: list[tuple[float, str]] = []
    for index, run in enumerate(runs):
        value = _float((run.get("metrics") or {}).get(key))
        if value is not None:
            values.append((value, run.get("run_id") or f"run {index + 1}"))
    if not values:
        return None
    values.sort(reverse=not reverse)
    return values[0][1]


def format_portfolio(data: dict[str, Any]) -> str:
    if not data.get("initialized"):
        hint = data.get("hint") or "python3 -m meridian paper --init"
        return (
            "Portefeuille paper non initialisé.\n"
            f"Pour l'initialiser : {hint}"
        )
    lines = [
        "Portefeuille paper",
        f"Période : {date(data.get('start'))} → {date(data.get('last_bar'))} "
        f"· {integer(data.get('runs'))} séance(s)",
        f"Equity {money(data.get('equity'))} · cash {money(data.get('cash'))} "
        f"({pct(data.get('cash_pct'), signed=False)}) · "
        f"exposition {pct(data.get('exposure'), signed=False)} · "
        f"DD {pct(data.get('drawdown'))}",
        f"Régime {data.get('regime') or 'n/a'} · "
        f"config {'inchangée' if data.get('config_match') else 'DÉRIVÉE'} · "
        f"source {data.get('data_source') or 'n/a'}",
    ]
    positions = data.get("positions") or []
    lines.append("")
    if positions:
        lines.append("Positions :")
        lines.append("Actif       Qté             PRU       Dernier       Poids       PnL")
        for position in positions:
            lines.append(
                f"{str(position.get('asset')):<11} "
                f"{num(position.get('qty'), 6):>13} "
                f"{money(position.get('avg_price'), 2):>10} "
                f"{money(position.get('last_price'), 2):>10} "
                f"{pct(position.get('weight'), signed=False):>8} "
                f"{money(position.get('unrealized_pnl')):>10}"
            )
    else:
        lines.append("Positions : aucune (100 % cash).")
    pending = data.get("pending_orders") or []
    if pending:
        lines.append("")
        lines.append("Intentions en attente :")
        for order in pending:
            lines.append(
                f"- {order.get('asset')} → {pct(order.get('target'), signed=False)}"
            )
    last_run = data.get("last_run")
    if last_run:
        lines.append("")
        lines.append(
            f"Dernière séance : {date(last_run.get('run_at'))} · "
            f"fills {integer(last_run.get('new_fills'))} · "
            f"régime {last_run.get('regime')}"
        )
    if data.get("memo_excerpt"):
        lines.append("")
        lines.append("Extrait du memo :")
        lines.append(data["memo_excerpt"])
    return "\n".join(lines)


def format_risk(data: dict[str, Any]) -> str:
    status = data.get("status") or "n/a"
    kill = bool(data.get("kill_switch"))
    lines = [
        f"Risque · {status}" + (" · KILL SWITCH ACTIF" if kill else " · kill switch inactif"),
        f"Généré : {data.get('generated_at') or 'n/a'}",
    ]
    info = data.get("data") or {}
    if info:
        lines.append(
            f"Données : {date(info.get('last_bar'))} · retard {info.get('stale_days')} j "
            f"· {info.get('assets_ok')}/{info.get('assets')} actifs OK"
        )
    paper = data.get("paper") or {}
    if paper:
        lines.append(
            f"Paper : equity {money(paper.get('equity'))} · "
            f"cash {money(paper.get('cash'))} · "
            f"DD {pct(paper.get('drawdown'))} · "
            f"{integer(paper.get('runs'))} séance(s)"
        )
    rolling = data.get("rolling") or {}
    if rolling:
        lines.append(
            f"Rolling {integer(rolling.get('days'))} j : "
            f"Sharpe {num(rolling.get('sharpe'))} · "
            f"CAGR {pct(rolling.get('cagr'))} · "
            f"Max DD {pct(rolling.get('max_drawdown'))}"
        )
    checks = data.get("checks") or []
    if checks:
        lines.append("")
        for check in checks:
            lines.append(
                f"[{check.get('status')}] {check.get('name')} — {check.get('value')}"
            )
            detail = check.get("detail")
            if detail:
                lines.append(f"      {detail}")
            action = check.get("action")
            if action and check.get("status") in {"WATCH", "ALERT"}:
                lines.append(f"      → {action}")
    replay = data.get("replay") or {}
    if replay:
        lines.append("")
        lines.append(
            f"Replay : dernière bougie {date(replay.get('last_bar'))} · "
            f"equity {money(replay.get('equity'))} · "
            f"DD max {pct(replay.get('max_drawdown'))}"
        )
    return "\n".join(lines)


def format_metric(data: dict[str, Any]) -> str:
    if not data.get("found"):
        available = ", ".join(data.get("available", []))
        return (
            f"Métrique inconnue : {data.get('metric')}.\n"
            f"Métriques disponibles : {available}."
        )
    lines = [
        f"{data.get('label')} ({data.get('metric')})",
        data.get("definition") or "",
        "",
        f"Lecture : {data.get('how_to_read') or 'n/a'}",
        f"Limite : {data.get('caveat') or 'n/a'}",
    ]
    return "\n".join(line for line in lines if line != "")


def format_usage(snapshot: dict[str, Any]) -> str:
    used = snapshot.get("used") or {}
    limits = snapshot.get("limits") or {}
    remaining = snapshot.get("remaining") or {}
    lines = [
        f"Usage local · {snapshot.get('date')}",
        f"Messages  : {integer(used.get('messages'))} / {integer(limits.get('messages'))} "
        f"(reste {integer(remaining.get('messages'))})",
        f"Backtests : {integer(used.get('backtests'))} / {integer(limits.get('backtests'))} "
        f"(reste {integer(remaining.get('backtests'))})",
        f"Tokens    : {integer(used.get('tokens'))} / {integer(limits.get('tokens'))} "
        f"(reste {integer(remaining.get('tokens'))})",
        f"Reset     : {snapshot.get('reset_at')}",
    ]
    if not snapshot.get("enabled", True):
        lines.append("Quotas désactivés pour cette session (--no-quota).")
    return "\n".join(lines)


def help_text() -> str:
    return (
        "Je peux :\n"
        "- lister les stratégies disponibles ;\n"
        "- lancer un backtest trend/momentum ou mean reversion ;\n"
        "- comparer deux backtests ;\n"
        "- afficher un backtest par son run_id ;\n"
        "- lire le portefeuille paper et le rapport de risque ;\n"
        "- expliquer une métrique (Sharpe, CAGR, drawdown, etc.) ;\n"
        "- afficher les quotas locaux.\n\n"
        "Exemples :\n"
        "- « Quelles stratégies as-tu ? »\n"
        "- « Fais un backtest trend momentum sur 2 ans »\n"
        "- « Compare trend et mean reversion sur 2 ans »\n"
        "- « Explique le Sharpe »\n"
        "- « Où en est le portefeuille paper ? »\n"
        "- « Quel est le risque actuel ? »\n\n"
        "Paper only. Aucun ordre réel, aucun conseil financier."
    )
