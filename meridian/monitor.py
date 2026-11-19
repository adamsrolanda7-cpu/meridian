"""Suivi opérationnel du desk : kill criteria, rolling, attribution, données.

Ce module ne prend aucune décision de marché et n'envoie aucun ordre. Il
compare l'état papier au replay déterministe du moteur, vérifie la fraîcheur
et la qualité des données, puis évalue des critères d'arrêt publiés à l'avance.

Un ``ALERT`` impose une action humaine : arrêter le paper, corriger les
données, ou réinitialiser avec un mandat documenté. Le module ne « répare »
jamais silencieusement un écart.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from . import paper
from .backtest import Backtest
from .config import ROOT, get, load_config
from .data import ensure_data, load_panel, quality_report
from .features import FeatureStore
from .metrics import performance

RUNS = os.path.join(ROOT, "runs")
MONITOR_JSON = os.path.join(RUNS, "monitor.json")
MONITOR_MD = os.path.join(RUNS, "monitor.md")

SEVERITY = {"OK": 0, "WATCH": 1, "ALERT": 2}


def _check(
    name: str,
    status: str,
    value: str,
    detail: str,
    action: str = "",
) -> dict:
    return {
        "name": name,
        "status": status,
        "value": value,
        "detail": detail,
        "action": action,
    }


def _pct(value: float | None, digits: int = 1, signed: bool = True) -> str:
    if value is None or not isinstance(value, (int, float, np.floating)) or not np.isfinite(value):
        return "n/a"
    pattern = f"{{:+.{digits}f}}%" if signed else f"{{:.{digits}f}}%"
    return pattern.format(value * 100).replace(".", ",")


def _num(value: float | None, digits: int = 2) -> str:
    if value is None or not isinstance(value, (int, float, np.floating)) or not np.isfinite(value):
        return "n/a"
    return f"{value:.{digits}f}".replace(".", ",")


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, float) and (not np.isfinite(obj)):
        return None
    return obj


def _attribution(trades: list[dict], since: pd.Timestamp | None = None) -> dict:
    sells = [
        t
        for t in trades
        if t.get("side") == "SELL"
        and (since is None or pd.Timestamp(t["ts"]) >= since)
    ]
    if not sells:
        return {"trades": 0, "pnl": 0.0, "by_sleeve": {}, "by_asset": {}}

    def agg(key: str) -> dict:
        out: dict[str, dict] = {}
        for trade in sells:
            label = str(trade.get(key) or "n/a")
            row = out.setdefault(label, {"trades": 0, "pnl": 0.0, "wins": 0})
            row["trades"] += 1
            row["pnl"] += float(trade.get("pnl") or 0.0)
            row["wins"] += int(float(trade.get("pnl") or 0.0) > 0)
        for row in out.values():
            row["win_rate"] = row["wins"] / row["trades"] if row["trades"] else 0.0
            row["pnl"] = round(row["pnl"], 2)
        return out

    return {
        "trades": len(sells),
        "pnl": round(sum(float(t.get("pnl") or 0.0) for t in sells), 2),
        "by_sleeve": agg("sleeve"),
        "by_asset": agg("asset"),
    }


def _positions_detail(result, equity: float) -> tuple[float, dict[str, float]]:
    snapshot = result.book.to_dict()
    weights: dict[str, float] = {}
    for asset, pos in snapshot.get("positions", {}).items():
        value = float(pos.get("qty", 0.0)) * float(pos.get("last_price", pos.get("avg_price", 0.0)))
        if equity > 0:
            weights[asset] = value / equity
    gross = sum(weights.values())
    return gross, weights


def evaluate(cfg: dict | None = None, refresh: bool = True) -> dict:
    """Construit l'état de suivi complet. Ne modifie pas le paper."""
    cfg = cfg or load_config()
    mon = cfg.get("monitor", {})
    now = datetime.now(timezone.utc)
    checks: list[dict] = []
    payload: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "config_hash": paper.config_hash(cfg),
        "checks": checks,
    }

    # ------------------------------------------------------------------ data
    try:
        panel, source = ensure_data(cfg, quiet=True) if refresh else (load_panel(cfg), "cache")
        quality = quality_report(cfg, panel)
        last_bar = max(df.index[-1] for df in panel.values())
        stale_days = max(0, int((now - last_bar).days))
    except Exception as exc:
        checks.append(
            _check(
                "données",
                "ALERT",
                "indisponibles",
                f"impossible de charger les données : {exc}",
                "corriger le cache ou relancer `python -m meridian download`.",
            )
        )
        payload.update({"status": "ALERT", "kill_switch": True, "source": "inconnue"})
        return _finalize(payload)

    payload.update(
        {
            "source": source,
            "last_bar": str(last_bar.date()),
            "stale_days": stale_days,
            "quality": quality,
        }
    )
    max_stale = int(get(cfg, "monitor.max_stale_days", 3))
    checks.append(
        _check(
            "fraîcheur données",
            "ALERT" if stale_days > max_stale else "OK",
            f"{stale_days} j",
            f"dernière bougie {last_bar.date()} · seuil {max_stale} j",
            "ne pas trader sur des données périmées ; relancer le téléchargement.",
        )
    )
    bad_quality = [s for s, q in quality.items() if q.get("status") != "OK"]
    checks.append(
        _check(
            "qualité données",
            "WATCH" if bad_quality else "OK",
            f"{len(bad_quality)} actif(s)" if bad_quality else "OK",
            "actifs à vérifier : " + ", ".join(bad_quality) if bad_quality else "tous les actifs OK",
            "exclure les actifs douteux ou documenter l'écart avant toute décision.",
        )
    )

    # ----------------------------------------------------------------- paper
    if not paper.state_exists():
        checks.append(
            _check(
                "état papier",
                "ALERT",
                "absent",
                "aucun état papier : le suivi n'a pas de portefeuille à auditer.",
                "initialiser avec `python -m meridian paper --init`.",
            )
        )
        payload.update({"status": "ALERT", "kill_switch": True})
        return _finalize(payload)

    state = paper.PaperState.load()
    payload["paper"] = {
        "start": state.start,
        "last_bar": state.last_bar,
        "equity": state.equity,
        "cash": state.cash,
        "drawdown": state.drawdown,
        "regime": state.last_regime,
        "runs": len(state.runs),
    }
    config_match = state.config_hash == paper.config_hash(cfg)
    checks.append(
        _check(
            "mandat",
            "OK" if config_match else "ALERT",
            "inchangé" if config_match else "DÉRIVÉ",
            "la config courante correspond à l'initialisation du paper"
            if config_match
            else "la config a changé depuis l'initialisation ; le paper ne représente plus le mandat publié.",
            "archiver l'état et réinitialiser avec `paper --init --force`, ou revenir à la config d'origine.",
        )
    )

    # ----------------------------------------------------------------- replay
    try:
        store = FeatureStore.build(panel, cfg)
        result = Backtest(store, cfg).run(
            start=state.start, initial_equity=state.initial_equity
        )
    except Exception as exc:
        checks.append(
            _check(
                "replay",
                "ALERT",
                "échec",
                f"le replay déterministe a échoué : {exc}",
                "ne pas continuer ; corriger le moteur ou les données.",
            )
        )
        payload.update({"status": "ALERT", "kill_switch": True})
        return _finalize(payload)

    equity = result.equity
    replay_last = str(equity.index[-1].date())
    replay_equity = float(equity.iloc[-1])
    divergence = (
        (state.equity - replay_equity) / replay_equity if replay_equity else float("nan")
    )
    same_bar = state.last_bar == replay_last
    tolerance = float(get(cfg, "monitor.max_replay_divergence", 0.01))
    if not same_bar:
        replay_status = "WATCH"
        replay_detail = (
            f"l'état papier s'arrête à {state.last_bar}, le replay à {replay_last} : "
            "une nouvelle bougie attend d'être traitée."
        )
        replay_action = "lancer `python -m meridian paper` pour rattraper la séance."
    elif abs(divergence) > tolerance:
        replay_status = "ALERT"
        replay_detail = (
            f"écart d'equity papier/replay {_pct(divergence, 2)} "
            f"(tolérance {_pct(tolerance, 2, signed=False)})."
        )
        replay_action = "auditer les fills, l'état et la config avant toute nouvelle séance."
    else:
        replay_status = "OK"
        replay_detail = f"equity papier/replay identiques à {_pct(divergence, 3)} près."
        replay_action = ""
    checks.append(_check("réconciliation replay", replay_status, _pct(divergence, 3), replay_detail, replay_action))

    current_dd = float(equity.iloc[-1] / equity.cummax().iloc[-1] - 1.0)
    max_dd = float((equity / equity.cummax() - 1.0).min())
    drawdown_watch = float(get(cfg, "monitor.drawdown_watch", 0.12))
    drawdown_kill = float(get(cfg, "monitor.drawdown_kill", 0.20))
    dd_status = (
        "ALERT"
        if current_dd <= -drawdown_kill
        else "WATCH"
        if current_dd <= -drawdown_watch
        else "OK"
    )
    checks.append(
        _check(
            "drawdown",
            dd_status,
            _pct(current_dd),
            f"drawdown courant · max historique du replay {_pct(max_dd)} · "
            f"watch {_pct(-drawdown_watch, signed=False)} · kill {_pct(-drawdown_kill, signed=False)}",
            "kill criteria atteint : arrêt du paper et revue humaine."
            if dd_status == "ALERT"
            else "surveiller ; ne pas augmenter le risque."
            if dd_status == "WATCH"
            else "",
        )
    )

    # -------------------------------------------------------- rolling metrics
    window = int(get(cfg, "monitor.rolling_window", 90))
    returns = equity.pct_change().dropna()
    rolling = returns.tail(window)
    min_history = int(get(cfg, "monitor.min_history_days", 60))
    if len(rolling) >= min_history:
        rolling_equity = (1.0 + rolling).cumprod() * state.initial_equity
        rolling_metrics = performance(rolling_equity)
        rolling_sharpe = float(rolling_metrics.get("sharpe") or 0.0)
        rolling_dd = float(rolling_metrics.get("max_drawdown") or 0.0)
        sharpe_watch = float(get(cfg, "monitor.rolling_sharpe_watch", 0.0))
        sharpe_kill = float(get(cfg, "monitor.rolling_sharpe_kill", -0.5))
        sharpe_status = (
            "ALERT"
            if rolling_sharpe <= sharpe_kill
            else "WATCH"
            if rolling_sharpe <= sharpe_watch
            else "OK"
        )
        checks.append(
            _check(
                "Sharpe glissant",
                sharpe_status,
                _num(rolling_sharpe),
                f"{len(rolling)} jours · DD fenêtre {_pct(rolling_dd)} · "
                f"watch {_num(sharpe_watch)} · kill {_num(sharpe_kill)}",
                "kill criteria atteint : suspension et revue du mandat."
                if sharpe_status == "ALERT"
                else "dégradation ; vérifier l'attribution avant de changer quoi que ce soit."
                if sharpe_status == "WATCH"
                else "",
            )
        )
        payload["rolling"] = {
            "window": window,
            "days": len(rolling),
            **_brief_metrics(rolling_metrics),
        }
    else:
        checks.append(
            _check(
                "Sharpe glissant",
                "WATCH",
                "historique court",
                f"{len(rolling)} jours seulement ; minimum {min_history}.",
                "attendre l'historique minimal avant d'interpréter le rolling.",
            )
        )

    # ------------------------------------------------------------- exposition
    gross, weights = _positions_detail(result, replay_equity)
    max_gross = float(get(cfg, "strategy.max_gross", 0.95))
    max_asset = float(get(cfg, "strategy.max_asset_weight", 0.35))
    breaches = {a: w for a, w in weights.items() if w > max_asset + 1e-6}
    exposure_ok = gross <= max_gross + 1e-6 and not breaches
    checks.append(
        _check(
            "exposition",
            "OK" if exposure_ok else "ALERT",
            _pct(gross),
            f"brut / plafond {_pct(max_gross, signed=False)} · positions {len(weights)} · "
            + (
                "dépassements : " + ", ".join(f"{a} {_pct(w)}" for a, w in breaches.items())
                if breaches
                else f"aucune position au-dessus de {_pct(max_asset, signed=False)}"
            ),
            "réduire l'exposition manuellement et corriger le portefeuille."
            if not exposure_ok
            else "",
        )
    )
    turnover = float(result.metrics.get("turnover_x") or 0.0)
    max_turnover = float(get(cfg, "monitor.max_turnover_x", 9.0))
    checks.append(
        _check(
            "turnover",
            "WATCH" if turnover > max_turnover else "OK",
            f"{_num(turnover, 1)}x",
            f"turnover annualisé · seuil {_num(max_turnover, 1)}x",
            "vérifier les coûts et la stabilité des signaux."
            if turnover > max_turnover
            else "",
        )
    )

    # --------------------------------------------------------------- gouverneur
    governor = result.governor
    halted = bool(governor.halted) if governor else False
    halt_dates = result.diagnostics.get("halt_dates", [])
    checks.append(
        _check(
            "gouverneur",
            "WATCH" if halted else "OK",
            "halt actif" if halted else "normal",
            f"drawdown gouverneur {_pct(governor.drawdown if governor else None)} · "
            f"dernier halt {halt_dates[-1] if halt_dates else 'aucun'}",
            "le halt est une protection, pas une panne : laisser le moteur sortir du halt."
            if halted
            else "",
        )
    )

    # ------------------------------------------------------------- attribution
    since = equity.index[-1] - pd.Timedelta(days=window)
    attribution = {
        "all": _attribution(result.trades),
        f"{window}d": _attribution(result.trades, since=since),
    }
    payload["attribution"] = attribution
    payload["replay"] = {
        "last_bar": replay_last,
        "equity": replay_equity,
        "max_drawdown": max_dd,
        "current_drawdown": current_dd,
        "gross": gross,
        "weights": weights,
        "regime": result.latest_decision.regime.label if result.latest_decision else "",
        "cash": float(result.book.cash),
        "halts": halt_dates,
    }
    payload["pending"] = state.pending_orders

    status = max((c["status"] for c in checks), key=lambda s: SEVERITY[s], default="OK")
    payload["status"] = status
    payload["kill_switch"] = any(
        c["status"] == "ALERT"
        and c["name"]
        in {
            "drawdown",
            "Sharpe glissant",
            "mandat",
            "réconciliation replay",
            "fraîcheur données",
            "exposition",
        }
        for c in checks
    )
    return _finalize(payload)


def _brief_metrics(metrics: dict) -> dict:
    keys = ("cagr", "volatility", "sharpe", "sortino", "max_drawdown", "turnover_x")
    return {k: metrics.get(k) for k in keys}


def _finalize(payload: dict) -> dict:
    payload = _jsonable(payload)
    return payload


def format_monitor(payload: dict) -> str:
    status = payload.get("status", "ALERT")
    lines = [
        "=" * 78,
        f"MERIDIAN — SUIVI OPÉRATIONNEL · {status}",
        "=" * 78,
        f"Généré le      : {payload.get('generated_at', 'n/a')}",
        f"Source données : {payload.get('source', 'n/a')} · dernière bougie {payload.get('last_bar', 'n/a')} "
        f"(retard {payload.get('stale_days', 'n/a')} j)",
    ]
    if payload.get("paper"):
        p = payload["paper"]
        equity_txt = f"{p['equity']:,.0f}".replace(",", " ")
        cash_txt = f"{p['cash']:,.0f}".replace(",", " ")
        lines.append(
            f"Paper          : {p['start']} → {p['last_bar']} · equity {equity_txt} $ "
            f"· cash {cash_txt} $ · DD {_pct(p['drawdown'])}"
        )
    roll = payload.get("rolling")
    if roll:
        lines.append(
            f"Rolling {roll['days']}j  : CAGR {_pct(roll.get('cagr'))} · "
            f"Sharpe {roll.get('sharpe', 0):.2f} · DD {_pct(roll.get('max_drawdown'))}"
        )
    lines.append("")
    lines.append(f"{'Critère':<24}{'Statut':<8}{'Valeur':<18}Détail")
    lines.append("-" * 78)
    for check in payload.get("checks", []):
        lines.append(
            f"{check['name']:<24}{check['status']:<8}{str(check['value']):<18}{check['detail']}"
        )
        if check.get("action") and check["status"] != "OK":
            lines.append(f"  → {check['action']}")
    attribution = payload.get("attribution", {}).get("all")
    if attribution:
        lines.append("")
        lines.append("Attribution (trades clos, depuis le début du paper) :")
        for sleeve, row in sorted(attribution.get("by_sleeve", {}).items()):
            lines.append(
                f"  {sleeve:<9} {row['trades']:>4} trades · PnL {row['pnl']:>+12,.0f} $ "
                f"· win {row['win_rate'] * 100:4.1f}%".replace(",", " ")
            )
    if payload.get("pending"):
        lines.append("")
        lines.append("Intentions en attente :")
        for order in payload["pending"]:
            lines.append(f"  {order['asset']:<9} cible {order['target'] * 100:5.1f}% · {order.get('reason', '')[:60]}")
    lines.append("")
    lines.append(
        "Kill switch : ACTIF — arrêt du paper et revue humaine."
        if payload.get("kill_switch")
        else "Kill switch : inactif — aucun critère d'arrêt déclenché."
    )
    lines.append("Aucun ordre réel n'a été envoyé.")
    return "\n".join(lines)


def write(payload: dict, json_path: str | None = None, md_path: str | None = None) -> tuple[str, str]:
    json_path = json_path or MONITOR_JSON
    md_path = md_path or MONITOR_MD
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, json_path)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(format_monitor(payload) + "\n")
    return json_path, md_path
