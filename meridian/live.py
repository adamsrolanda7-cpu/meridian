"""Tableau de bord terminal du desk Meridian.

Le module ne prend aucune décision de marché et n'envoie aucun ordre. Il
affiche l'état du paper, le dernier snapshot du moteur, les signaux, les
positions, les intentions et le suivi opérationnel. Le rendu utilise le
terminal (curses), pas le web.

Deux modes :
- ``curses`` : plein écran, rafraîchi en continu, touches interactives ;
- ``plain`` : une ou plusieurs trames texte, utile dans un journal ou un
  second terminal sans TUI.

Le dashboard peut exécuter le paper **uniquement lorsqu'une nouvelle bougie
clôturée est disponible**. Il ne crée pas de position rétroactive et ne
contourne jamais le kill switch.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd

from . import monitor, paper
from .backtest import Backtest
from .config import ROOT, get, load_config
from .data import DataError, download_all, ensure_data, load_panel, quality_report
from .features import FeatureStore

MONITOR_JSON = os.path.join(ROOT, "runs", "monitor.json")
JOURNAL_PATH = os.path.join(ROOT, "runs", "paper", "journal.jsonl")


# --------------------------------------------------------------------- format
def _pct(value: Any, digits: int = 1, signed: bool = True) -> str:
    if value is None or not isinstance(value, (int, float)) or not pd.notna(value):
        return "n/a"
    pattern = f"{{:+.{digits}f}}%" if signed else f"{{:.{digits}f}}%"
    return pattern.format(float(value) * 100).replace(".", ",")


def _money(value: Any, digits: int = 0) -> str:
    if value is None or not isinstance(value, (int, float)) or not pd.notna(value):
        return "n/a"
    return f"{float(value):,.{digits}f}".replace(",", " ").replace(".", ",")


def _price(value: Any) -> str:
    if value is None or not isinstance(value, (int, float)) or not pd.notna(value):
        return "n/a"
    value = float(value)
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 1:
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{value:.4f}".replace(".", ",")


def _score(value: Any) -> str:
    if value is None or not isinstance(value, (int, float)) or not pd.notna(value):
        return "—"
    return f"{float(value):.2f}".replace(".", ",")


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}j {hours:02d}h {minutes:02d}m"
    return f"{hours:02d}h {minutes:02d}m {secs:02d}s"


def _next_daily_close(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _seconds_until(iso: str | None, now: datetime | None = None) -> float | None:
    if not iso:
        return None
    try:
        target = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (target - now).total_seconds())


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _tail_journal(limit: int = 8, start: str | None = None) -> list[dict]:
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-max(limit * 4, limit):]
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if start and str(entry.get("start")) != str(start):
                continue
            out.append(entry)
        return out[-limit:]
    except Exception:
        return []


def _quality_ok(quality: dict) -> tuple[int, int, list[str]]:
    total = len(quality)
    bad = [symbol for symbol, row in quality.items() if row.get("status") != "OK"]
    return total - len(bad), total, bad


def _activity(events: Iterable[dict] | None, journal: list[dict]) -> list[dict]:
    out: list[dict] = []
    for entry in journal[-6:]:
        raw_ts = str(entry.get("run_at", ""))
        ts = raw_ts[11:19] if "T" in raw_ts else raw_ts[:8]
        out.append(
            {
                "ts": ts,
                "level": "info",
                "message": (
                    f"séance {entry.get('last_bar', '?')} · "
                    f"equity {_money(entry.get('equity'), 0)} $ · "
                    f"{entry.get('new_fills', 0)} fill(s) · "
                    f"{(entry.get('regime') or 'n/a')}"
                ),
            }
        )
    for event in events or []:
        raw_ts = str(event.get("ts", ""))
        out.append(
            {
                **event,
                "ts": raw_ts[11:19] if "T" in raw_ts else raw_ts[:8],
            }
        )
    out.sort(key=lambda item: str(item.get("ts", "")))
    return out[-12:]


# --------------------------------------------------------------------- snapshot
def _align_panel(panel: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Tronque le panel à la dernière bougie présente pour tous les actifs.

    Un téléchargement partiel peut laisser certains CSV en avance. Le
    dashboard doit alors afficher la dernière bougie commune au lieu de
    calculer des signaux sur une date absente chez un actif.
    """
    if not panel:
        raise DataError("aucun actif dans le panel")
    last = min(df.index[-1] for df in panel.values())
    return {
        symbol: df.loc[:last] if df.index[-1] > last else df
        for symbol, df in panel.items()
    }


def collect_snapshot(
    cfg: dict | None = None,
    include_monitor: bool = False,
    events: Iterable[dict] | None = None,
    offline: bool = False,
) -> dict:
    """Construit un snapshot complet du desk, sans modifier le paper.

    ``include_monitor=True`` recalcule le monitor (plus coûteux). Sinon, le
    dernier ``runs/monitor.json`` est réutilisé pour l'affichage.

    ``offline=True`` interdit tout téléchargement : seuls les fichiers déjà
    présents dans ``data/`` sont lus. Si le cache est absent, l'erreur est
    explicite au lieu de déclencher une requête réseau implicite.
    """
    cfg = cfg or load_config()
    now = datetime.now(timezone.utc)
    if offline:
        panel = load_panel(cfg)
        source = "cache (offline)"
    else:
        panel, source = ensure_data(cfg, quiet=True)
    panel = _align_panel(panel)
    quality = quality_report(cfg, panel)
    last_bar = max(df.index[-1] for df in panel.values())
    last_prices = {symbol: float(df["close"].iloc[-1]) for symbol, df in panel.items()}
    ok_assets, total_assets, bad_assets = _quality_ok(quality)

    state = paper.PaperState.load() if paper.state_exists() else None
    cached = _read_json(paper.SNAPSHOT_PATH) if state else None
    cache_ok = bool(
        cached
        and state
        and cached.get("config_hash") == state.config_hash
        and cached.get("start") == state.start
        and (
            cached.get("last_bar") == str(last_bar.date())
            or (
                str(last_bar.date()) == state.last_bar
                and cached.get("last_bar") == state.last_bar
            )
        )
    )

    regime: dict = {}
    signal_rows: list[dict] = []
    target_rows: list[dict] = []
    portfolio_vol = 0.0
    replay_start = state.start if state else None
    replay_end = state.last_bar if state else str(last_bar.date())
    replay_equity = float(state.equity) if state else float(cfg.get("initial_equity", 100_000.0))
    new_bar = bool(state and str(last_bar.date()) > state.last_bar)

    if cache_ok:
        regime = dict(cached.get("regime") or {})
        signal_rows = list(cached.get("signals") or [])
        target_rows = list(cached.get("targets") or [])
        portfolio_vol = float(cached.get("portfolio_vol") or 0.0)
        replay_start = cached.get("start", replay_start)
        replay_end = cached.get("last_bar", replay_end)
        replay_equity = float(cached.get("equity") or replay_equity)
    elif state:
        store = FeatureStore.build(panel, cfg)
        result = Backtest(store, cfg).run(
            start=state.start,
            initial_equity=state.initial_equity,
        )
        decision = result.latest_decision
        regime = decision.regime.as_dict() if decision else {}
        signal_rows = [s.as_dict() for s in decision.signals] if decision else []
        target_rows = [t.as_dict() for t in decision.targets] if decision else []
        portfolio_vol = float(decision.portfolio_vol) if decision else 0.0
        replay_start = result.start
        replay_end = result.end
        replay_equity = float(result.equity.iloc[-1])
        new_bar = bool(result.end > state.last_bar)
        cache = {
            "version": 1,
            "run_at": now.isoformat(timespec="seconds"),
            "config_hash": state.config_hash,
            "start": state.start,
            "last_bar": result.end,
            "equity": replay_equity,
            "regime": regime,
            "signals": signal_rows,
            "targets": target_rows,
            "orders": [order.__dict__ for order in decision.orders] if decision else [],
            "portfolio_vol": portfolio_vol,
        }
        try:
            paper._write_json(paper.SNAPSHOT_PATH, cache)
        except Exception:
            pass

    signals: dict[str, dict[str, float]] = {
        asset: {"trend": 0.0, "meanrev": 0.0, "xsmom": 0.0} for asset in cfg.get("universe", [])
    }
    for signal in signal_rows:
        asset = str(signal.get("asset"))
        sleeve = str(signal.get("sleeve"))
        row = signals.setdefault(asset, {"trend": 0.0, "meanrev": 0.0, "xsmom": 0.0})
        row[sleeve] = float(signal.get("score") or 0.0)
    targets = {str(t.get("asset")): float(t.get("weight") or 0.0) for t in target_rows}

    positions = dict(state.positions) if state else {}
    weights: dict[str, float] = {}
    equity = float(state.equity) if state else float(cfg.get("initial_equity", 100_000.0))
    for asset, pos in positions.items():
        notional = pos.get("notional")
        if notional is None:
            notional = float(pos.get("qty", 0.0)) * last_prices.get(asset, 0.0)
        if equity > 0:
            weights[asset] = float(notional) / equity
    gross = sum(weights.values())
    pending = list(state.pending_orders) if state else []
    pending_map = {str(order.get("asset")): float(order.get("target", 0.0)) for order in pending}

    if state:
        paper_payload = {
            "exists": True,
            "start": state.start,
            "last_bar": state.last_bar,
            "equity": state.equity,
            "cash": state.cash,
            "drawdown": state.drawdown,
            "total_return": state.equity / state.initial_equity - 1.0 if state.initial_equity else None,
            "gross": gross,
            "positions": positions,
            "weights": weights,
            "pending": pending,
            "pending_map": pending_map,
            "config_match": state.config_hash == paper.config_hash(cfg),
            "runs": len(state.runs),
        }
    else:
        paper_payload = {
            "exists": False,
            "start": None,
            "last_bar": None,
            "equity": equity,
            "cash": equity,
            "drawdown": 0.0,
            "total_return": None,
            "gross": gross,
            "positions": {},
            "weights": {},
            "pending": [],
            "pending_map": {},
            "config_match": False,
            "runs": 0,
        }

    monitor_payload = None
    if include_monitor:
        try:
            monitor_payload = monitor.evaluate(cfg, refresh=False)
        except Exception as exc:
            monitor_payload = {
                "status": "ALERT",
                "kill_switch": True,
                "checks": [],
                "error": str(exc),
            }
    else:
        monitor_payload = _read_json(MONITOR_JSON)
        if monitor_payload:
            monitor_payload = dict(monitor_payload)
            monitor_payload["cached"] = True

    status = (monitor_payload or {}).get("status", "OK")
    if not state:
        status = "ALERT"
    elif not paper_payload["config_match"]:
        status = "ALERT"

    event_list = list(events or [])
    event_list.append(
        {
            "ts": now.strftime("%H:%M:%S"),
            "level": "info",
            "message": (
                f"snapshot · données {last_bar.date()} · "
                f"{ok_assets}/{total_assets} actifs OK · {source}"
            ),
        }
    )
    journal = _tail_journal(start=state.start if state else None)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status,
        "meanrev_enabled": bool(get(cfg, "strategy.meanrev.enabled", False)),
        "data": {
            "source": source,
            "last_bar": str(last_bar.date()),
            "stale_days": max(0, int((now - last_bar).days)),
            "quality": quality,
            "ok_assets": ok_assets,
            "total_assets": total_assets,
            "bad_assets": bad_assets,
            "last_prices": last_prices,
        },
        "next_close": _next_daily_close(now).isoformat(timespec="seconds"),
        "regime": regime,
        "paper": paper_payload,
        "signals": signals,
        "targets": targets,
        "pending_map": pending_map,
        "portfolio_vol": portfolio_vol,
        "monitor": monitor_payload,
        "journal": journal,
        "activity": _activity(event_list, journal),
        "replay": {
            "start": replay_start,
            "end": replay_end,
            "equity": replay_equity,
            "new_bar": new_bar,
        },
    }


# --------------------------------------------------------------------- render
def _style_for_status(status: str) -> str:
    return {"OK": "ok", "WATCH": "watch", "ALERT": "alert"}.get(str(status).upper(), "normal")


def _separator(title: str, width: int) -> tuple[str, str]:
    prefix = f"── {title} "
    return (prefix + "─" * max(0, width - len(prefix)), "section")


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text[:width]


def dashboard_lines(
    snapshot: dict,
    width: int = 100,
    auto_run: bool = True,
    fetch: bool = False,
    offline: bool = False,
) -> list[tuple[str, str]]:
    """Construit les lignes du dashboard, sans footer ni pagination."""
    width = max(40, int(width))
    lines: list[tuple[str, str]] = []
    add = lambda text, style="normal": lines.append((_clip(text, width), style))

    status = str(snapshot.get("status", "n/a")).upper()
    style = _style_for_status(status)
    data = snapshot.get("data", {})
    regime = snapshot.get("regime", {})
    paper_payload = snapshot.get("paper", {})
    monitor_payload = snapshot.get("monitor") or {}
    clock = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    snapshot_at = str(snapshot.get("generated_at", ""))[11:19]

    add("=" * width, "dim")
    modes = (
        "OFFLINE · cache local uniquement"
        if offline
        else f"AUTO {'ON' if auto_run else 'OFF'} · FETCH {'ON' if fetch else 'OFF'}"
    )
    add(
        f" MERIDIAN · LIVE PAPER   {clock} UTC   [{status}]   {modes}",
        style if status != "OK" else "title",
    )
    add(f" Aucun ordre réel — mode paper uniquement · snapshot {snapshot_at} UTC", "dim")

    last_bar = data.get("last_bar", "n/a")
    stale = data.get("stale_days", "n/a")
    next_close = snapshot.get("next_close")
    until = _seconds_until(next_close)
    next_txt = _fmt_duration(until) if until is not None else "n/a"
    add(
        f" Données {last_bar} · retard {stale} j · {data.get('source', 'n/a')} · "
        f"{data.get('ok_assets', 0)}/{data.get('total_assets', 0)} actifs OK · "
        f"clôture dans {next_txt}",
        "dim",
    )
    if data.get("bad_assets"):
        add(f" ! Actifs à vérifier : {', '.join(data['bad_assets'])}", "alert")

    btc = (data.get("last_prices") or {}).get("BTCUSDT")
    if btc is not None:
        add(f" BTC {_price(btc)} $ · régime {regime.get('label', 'n/a')}", "accent")

    add("", "normal")
    lines.append(_separator("MARCHÉ", width))
    label = str(regime.get("label", "n/a"))
    regime_style = {
        "TREND_UP": "ok",
        "RANGE": "accent",
        "TREND_DOWN": "alert",
        "CRISIS": "alert",
        "WARMUP": "dim",
    }.get(label, "normal")
    add(
        f" Régime {label} · confiance {_pct(regime.get('confidence'), 0, signed=False)} · "
        f"breadth {_pct(regime.get('breadth'), 0, signed=False)} · "
        f"ADX {regime.get('adx', 0):.0f} · vol_pct {_pct(regime.get('vol_pct'), 0, signed=False)}",
        regime_style,
    )
    if regime.get("reason"):
        add(f" {regime['reason']}", "dim")

    add("", "normal")
    lines.append(_separator("PORTEFEUILLE PAPER", width))
    if not paper_payload.get("exists"):
        add(" Paper non initialisé — lancez `python3 -m meridian paper --init`.", "alert")
    else:
        add(
            f" Equity {_money(paper_payload.get('equity'))} $ · "
            f"cash {_money(paper_payload.get('cash'))} $ · "
            f"exposition {_pct(paper_payload.get('gross'), 1, signed=False)} · "
            f"DD {_pct(paper_payload.get('drawdown'))} · "
            f"rendement {_pct(paper_payload.get('total_return'))}",
            "bold",
        )
        add(
            f" Séance {paper_payload.get('last_bar', 'n/a')} · "
            f"positions {len(paper_payload.get('positions', {}))} · "
            f"intentions {len(paper_payload.get('pending', []))} · "
            f"config {'OK' if paper_payload.get('config_match') else 'DÉRIVÉE'} · "
            f"runs {paper_payload.get('runs', 0)}",
            "normal" if paper_payload.get("config_match") else "alert",
        )

    add("", "normal")
    lines.append(_separator("SIGNAUX DU MOTEUR", width))
    header = f" {'Actif':<10}{'Dernier':>13}{'Tend.':>8}{'MR':>8}{'XS':>8}{'Cible':>9}"
    add(header, "dim")
    prices = data.get("last_prices", {})
    meanrev_enabled = bool(snapshot.get("meanrev_enabled", False))
    for asset in sorted(snapshot.get("signals", {})):
        row = snapshot["signals"].get(asset, {})
        target = snapshot.get("targets", {}).get(asset, 0.0)
        mr_txt = _score(row.get("meanrev")) if meanrev_enabled else "off"
        add(
            f" {asset:<10}{_price(prices.get(asset)):>13}"
            f"{_score(row.get('trend')):>8}{mr_txt:>8}"
            f"{_score(row.get('xsmom')):>8}{_pct(target, 1, signed=False):>9}",
            "normal",
        )

    positions = paper_payload.get("positions", {})
    pending = paper_payload.get("pending", [])
    add("", "normal")
    lines.append(_separator("POSITIONS & INTENTIONS", width))
    if positions:
        for asset, pos in sorted(positions.items()):
            weight = (paper_payload.get("weights") or {}).get(asset, 0.0)
            add(
                f" {asset:<10} qty {float(pos.get('qty', 0)):.6f} · "
                f"PRU {_price(pos.get('avg_price'))} · stop {_price(pos.get('stop'))} · "
                f"poids {_pct(weight, 1, signed=False)} · {pos.get('sleeve', '—')}",
                "normal",
            )
    else:
        add(" Aucune position ouverte.", "dim")
    if pending:
        for order in pending:
            reason = str(order.get("reason", ""))[: max(10, width - 38)]
            add(
                f" → {order.get('asset', '?'):<10} cible "
                f"{_pct(order.get('target'), 1, signed=False)} · {reason}",
                "accent",
            )
    else:
        add(" Aucune intention en attente pour la prochaine ouverture.", "dim")

    add("", "normal")
    lines.append(_separator("MONITOR & KILL CRITERIA", width))
    mon_status = str(monitor_payload.get("status", "n/a")).upper()
    kill = "ON" if monitor_payload.get("kill_switch") else "off"
    cached = " · cache" if monitor_payload.get("cached") else ""
    add(
        f" Statut {mon_status} · kill switch {kill}{cached} · "
        f"{len(monitor_payload.get('checks', []))} contrôles",
        _style_for_status(mon_status),
    )
    if monitor_payload.get("error"):
        add(f" ! {monitor_payload['error']}", "alert")
    shown = 0
    for check in monitor_payload.get("checks", []):
        if check.get("status") == "OK":
            continue
        add(
            f" {check.get('status', '?'):<6} {check.get('name', '?'):<22} "
            f"{check.get('value', '')} · {check.get('detail', '')}",
            _style_for_status(check.get("status", "")),
        )
        shown += 1
        if shown >= 4:
            break
    if not monitor_payload.get("checks"):
        add(" Monitor indisponible : lancez `python3 -m meridian monitor`.", "dim")

    add("", "normal")
    lines.append(_separator("ACTIVITÉ RÉCENTE", width))
    activity = snapshot.get("activity", [])
    if not activity:
        add(" Aucun événement.", "dim")
    for event in activity[-6:]:
        level = str(event.get("level", "info"))
        style = {"ok": "ok", "warn": "watch", "alert": "alert"}.get(level, "dim")
        add(f" {str(event.get('ts', ''))[:8]:<8} {event.get('message', '')}", style)

    return lines


def format_dashboard(
    snapshot: dict,
    width: int = 100,
    height: int | None = None,
    scroll: int = 0,
    auto_run: bool = True,
    fetch: bool = False,
    offline: bool = False,
) -> list[tuple[str, str]]:
    """Rend le dashboard paginé. Retourne des couples ``(texte, style)``."""
    width = max(40, int(width))
    content = dashboard_lines(
        snapshot, width=width, auto_run=auto_run, fetch=fetch, offline=offline
    )
    footer = (
        " q quitter · r rafraîchir · p exécuter paper · ↑↓ défiler · OFFLINE "
        if offline
        else " q quitter · r rafraîchir · p exécuter paper · d télécharger · "
        "a auto · f fetch · ↑↓ défiler "
    )
    footer = _clip(footer, width)
    if height is None or height <= 0:
        return content + [(footer, "dim")]

    height = max(4, int(height))
    body_height = max(1, height - 1)
    max_scroll = max(0, len(content) - body_height)
    scroll = min(max(0, int(scroll)), max_scroll)
    body = content[scroll : scroll + body_height]
    if scroll > 0 or max_scroll > 0:
        position = f" {scroll + 1}-{min(scroll + body_height, len(content))}/{len(content)} "
        footer = _clip(footer[:-1] + position, width)
    if len(body) < body_height:
        body = body + [("", "normal")] * (body_height - len(body))
    return body + [(footer, "dim")]


def render_text(
    snapshot: dict,
    width: int = 100,
    height: int | None = None,
    scroll: int = 0,
    auto_run: bool = True,
    fetch: bool = False,
    offline: bool = False,
) -> str:
    return "\n".join(
        text
        for text, _ in format_dashboard(
            snapshot, width, height, scroll, auto_run, fetch, offline
        )
    )


# --------------------------------------------------------------------- curses
_ATTRS: dict[str, int] = {}


def _init_colors() -> None:
    import curses

    _ATTRS.clear()
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:
        background = curses.COLOR_BLACK
    curses.init_pair(1, curses.COLOR_CYAN, background)
    curses.init_pair(2, curses.COLOR_GREEN, background)
    curses.init_pair(3, curses.COLOR_YELLOW, background)
    curses.init_pair(4, curses.COLOR_RED, background)
    curses.init_pair(5, curses.COLOR_MAGENTA, background)
    curses.init_pair(6, curses.COLOR_WHITE, background)
    _ATTRS.update(
        {
            "title": curses.color_pair(1) | curses.A_BOLD,
            "section": curses.color_pair(1) | curses.A_BOLD,
            "ok": curses.color_pair(2) | curses.A_BOLD,
            "watch": curses.color_pair(3) | curses.A_BOLD,
            "alert": curses.color_pair(4) | curses.A_BOLD,
            "accent": curses.color_pair(5),
            "bold": curses.color_pair(6) | curses.A_BOLD,
            "dim": curses.A_DIM,
            "normal": curses.A_NORMAL,
        }
    )


def _attr(style: str) -> int:
    return _ATTRS.get(style, 0)


def _draw(
    stdscr,
    snapshot: dict,
    auto_run: bool,
    fetch: bool,
    scroll: int,
    offline: bool = False,
) -> None:
    import curses

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    lines = format_dashboard(
        snapshot,
        width=max(20, width - 1),
        height=max(3, height),
        scroll=scroll,
        auto_run=auto_run,
        fetch=fetch,
        offline=offline,
    )
    for y, (text, style) in enumerate(lines[:height]):
        try:
            stdscr.addnstr(y, 0, text, max(0, width - 1), _attr(style))
        except curses.error:
            pass
    try:
        stdscr.refresh()
    except curses.error:
        pass


def _run_paper_if_new(cfg: dict, events: deque, quiet: bool = False) -> bool:
    state = paper.PaperState.load() if paper.state_exists() else None
    if state is None:
        events.append(
            {"ts": datetime.now().strftime("%H:%M:%S"), "level": "alert", "message": "paper non initialisé"}
        )
        return False
    try:
        panel = load_panel(cfg)
        last = max(df.index[-1] for df in panel.values())
        if state.last_bar >= str(last.date()):
            if not quiet:
                events.append(
                    {
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "info",
                        "message": "paper déjà à jour · aucune nouvelle bougie",
                    }
                )
            return False
        summary = paper.run_paper(cfg, quiet=True, refresh=False)
        events.append(
            {
                "ts": datetime.now().strftime("%H:%M:%S"),
                "level": "ok" if summary.get("new_fills", 0) else "info",
                "message": (
                    f"paper exécuté · {summary.get('last_bar')} · "
                    f"{summary.get('new_fills', 0)} fill(s) · "
                    f"equity {_money(summary.get('equity'))} $"
                ),
            }
        )
        return True
    except Exception as exc:
        events.append(
            {"ts": datetime.now().strftime("%H:%M:%S"), "level": "alert", "message": f"paper: {exc}"}
        )
        return False


def _refresh_monitor(cfg: dict, events: deque, write: bool) -> dict | None:
    try:
        payload = monitor.evaluate(cfg, refresh=False)
        if write:
            monitor.write(payload)
        events.append(
            {
                "ts": datetime.now().strftime("%H:%M:%S"),
                "level": "ok" if payload.get("status") == "OK" else "warn",
                "message": f"monitor {payload.get('status')} · kill {bool(payload.get('kill_switch'))}",
            }
        )
        return payload
    except Exception as exc:
        events.append(
            {"ts": datetime.now().strftime("%H:%M:%S"), "level": "alert", "message": f"monitor: {exc}"}
        )
        return None


def _curses_loop(
    stdscr,
    cfg: dict,
    interval: float,
    snapshot_interval: float,
    monitor_interval: float,
    auto_run: bool,
    fetch: bool,
    fetch_interval: float,
    write_monitor: bool,
    offline: bool = False,
) -> int:
    import curses

    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.nodelay(True)
    stdscr.timeout(250)
    stdscr.keypad(True)
    _init_colors()

    events: deque = deque(maxlen=30)
    events.append(
        {"ts": datetime.now().strftime("%H:%M:%S"), "level": "info", "message": "dashboard démarré"}
    )
    auto = bool(auto_run)
    fetch_on = bool(fetch) and not offline
    scroll = 0
    monitor_payload = _read_json(MONITOR_JSON)
    if monitor_payload:
        monitor_payload = dict(monitor_payload)
        monitor_payload["cached"] = True
    snapshot: dict = {}
    last_snapshot = 0.0
    last_monitor = time.monotonic()
    last_fetch = 0.0
    force = True

    while True:
        now_m = time.monotonic()
        if force or now_m - last_snapshot >= max(1.0, snapshot_interval):
            ran_paper = _run_paper_if_new(cfg, events) if auto else False
            try:
                snapshot = collect_snapshot(
                    cfg, include_monitor=False, events=list(events), offline=offline
                )
            except Exception as exc:
                snapshot = {
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "status": "ALERT",
                    "data": {},
                    "regime": {},
                    "paper": {"exists": False},
                    "signals": {},
                    "monitor": monitor_payload,
                    "activity": [
                        {"ts": datetime.now().strftime("%H:%M:%S"), "level": "alert", "message": str(exc)}
                    ],
                }
                events.append(
                    {"ts": datetime.now().strftime("%H:%M:%S"), "level": "alert", "message": f"snapshot: {exc}"}
                )
            if ran_paper:
                updated = _refresh_monitor(cfg, events, write_monitor)
                if updated:
                    monitor_payload = updated
            if monitor_payload:
                snapshot["monitor"] = monitor_payload
            last_snapshot = now_m
            force = False

        if now_m - last_monitor >= max(5.0, monitor_interval):
            updated = _refresh_monitor(cfg, events, write_monitor)
            if updated:
                monitor_payload = updated
                snapshot["monitor"] = updated
            last_monitor = now_m

        if fetch_on and not offline and now_m - last_fetch >= max(30.0, fetch_interval):
            try:
                download_all(cfg, quiet=True, force=True)
                events.append(
                    {
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "ok",
                        "message": "données téléchargées · cache actualisé",
                    }
                )
                force = True
            except Exception as exc:
                events.append(
                    {"ts": datetime.now().strftime("%H:%M:%S"), "level": "alert", "message": f"download: {exc}"}
                )
            last_fetch = now_m

        _draw(stdscr, snapshot, auto, fetch_on, scroll, offline)
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (ord("q"), ord("Q"), 27):
            break
        if ch in (ord("r"), ord("R")):
            force = True
            updated = _refresh_monitor(cfg, events, write_monitor)
            if updated:
                monitor_payload = updated
            continue
        if ch in (ord("p"), ord("P")):
            if _run_paper_if_new(cfg, events):
                force = True
                updated = _refresh_monitor(cfg, events, write_monitor)
                if updated:
                    monitor_payload = updated
            continue
        if ch in (ord("d"), ord("D")):
            if offline:
                events.append(
                    {
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "warn",
                        "message": "téléchargement désactivé · mode offline",
                    }
                )
                continue
            try:
                download_all(cfg, quiet=True, force=True)
                events.append(
                    {
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "ok",
                        "message": "données téléchargées · cache actualisé",
                    }
                )
                force = True
            except Exception as exc:
                events.append(
                    {"ts": datetime.now().strftime("%H:%M:%S"), "level": "alert", "message": f"download: {exc}"}
                )
            continue
        if ch in (ord("a"), ord("A")):
            auto = not auto
            events.append(
                {
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "level": "info",
                    "message": f"auto-run {'ON' if auto else 'OFF'}",
                }
            )
            continue
        if ch in (ord("f"), ord("F")):
            if offline:
                events.append(
                    {
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "warn",
                        "message": "fetch désactivé · mode offline",
                    }
                )
                continue
            fetch_on = not fetch_on
            events.append(
                {
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "level": "info",
                    "message": f"fetch {'ON' if fetch_on else 'OFF'}",
                }
            )
            continue
        if ch in (curses.KEY_UP, ord("k")):
            scroll = max(0, scroll - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            scroll += 1
        elif ch == curses.KEY_PPAGE:
            scroll = max(0, scroll - 10)
        elif ch == curses.KEY_NPAGE:
            scroll += 10
        elif ch == curses.KEY_HOME:
            scroll = 0
        elif ch == curses.KEY_END:
            scroll = 10_000
        elif ch == curses.KEY_RESIZE:
            scroll = 0
    return 0


# --------------------------------------------------------------------- launch
def _child_args(
    interval: float,
    snapshot_interval: float,
    monitor_interval: float,
    auto_run: bool,
    fetch: bool,
    fetch_interval: float,
    write_monitor: bool,
    config_path: str | None,
    offline: bool = False,
) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "meridian",
        "live",
        "--interval",
        str(interval),
        "--snapshot-interval",
        str(snapshot_interval),
        "--monitor-interval",
        str(monitor_interval),
        "--fetch-interval",
        str(fetch_interval),
    ]
    if not auto_run:
        args.append("--no-auto-run")
    if fetch:
        args.append("--fetch")
    if offline:
        args.append("--offline")
    if not write_monitor:
        args.append("--no-write-monitor")
    if config_path:
        args.extend(["--config", config_path])
    return args


def launch_window(
    interval: float = 5.0,
    snapshot_interval: float = 30.0,
    monitor_interval: float = 300.0,
    auto_run: bool = True,
    fetch: bool = False,
    fetch_interval: float = 300.0,
    write_monitor: bool = True,
    config_path: str | None = None,
    offline: bool = False,
) -> int:
    """Tente d'ouvrir le dashboard dans une nouvelle fenêtre de terminal.

    Retourne 0 si une fenêtre a été lancée, 1 sinon. Aucun échec n'est fatal :
    l'appelant peut alors démarrer le dashboard dans le terminal courant.
    """
    args = _child_args(
        interval,
        snapshot_interval,
        monitor_interval,
        auto_run,
        fetch,
        fetch_interval,
        write_monitor,
        config_path,
        offline,
    )
    shell_cmd = f"cd {shlex.quote(ROOT)} && exec {shlex.join(args)}"
    if os.environ.get("TMUX") and shutil.which("tmux"):
        try:
            subprocess.Popen(["tmux", "new-window", "-n", "meridian-live", shell_cmd])
            return 0
        except Exception:
            pass
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return 1
    candidates = [
        ("gnome-terminal", ["--", "bash", "-lc", shell_cmd]),
        ("konsole", ["-e", "bash", "-lc", shell_cmd]),
        ("xfce4-terminal", ["-e", "bash", "-lc", shell_cmd]),
        ("xterm", ["-e", "bash", "-lc", shell_cmd]),
        ("alacritty", ["-e", "bash", "-lc", shell_cmd]),
        ("kitty", ["bash", "-lc", shell_cmd]),
    ]
    for name, extra in candidates:
        if not shutil.which(name):
            continue
        try:
            subprocess.Popen([name, *extra])
            return 0
        except Exception:
            continue
    return 1


# --------------------------------------------------------------------- entry
def run(
    cfg: dict | None = None,
    interval: float = 5.0,
    snapshot_interval: float = 30.0,
    monitor_interval: float = 300.0,
    auto_run: bool = True,
    fetch: bool = False,
    fetch_interval: float = 300.0,
    write_monitor: bool = True,
    plain: bool = False,
    follow: bool = False,
    window: bool = False,
    config_path: str | None = None,
    offline: bool = False,
) -> int:
    """Point d'entrée du dashboard terminal."""
    cfg = cfg or load_config()
    if window:
        if launch_window(
            interval,
            snapshot_interval,
            monitor_interval,
            auto_run,
            fetch,
            fetch_interval,
            write_monitor,
            config_path,
            offline,
        ) == 0:
            return 0
        print("Fenêtre de terminal indisponible ; démarrage dans ce terminal.")
        print("Pour un second terminal : python3 -m meridian live")
        print()

    if plain or follow or not sys.stdout.isatty():
        frames = 0
        events: deque = deque(maxlen=30)
        while True:
            if auto_run and follow and not offline:
                _run_paper_if_new(cfg, events, quiet=True)
            try:
                snapshot = collect_snapshot(
                    cfg, include_monitor=False, events=list(events), offline=offline
                )
            except Exception as exc:
                print(f"ERREUR : {exc}", file=sys.stderr)
                if offline:
                    print(
                        "Le mode offline ne télécharge rien. Pour actualiser le cache : "
                        "python3 -m meridian download",
                        file=sys.stderr,
                    )
                return 1
            size = shutil.get_terminal_size((100, 40))
            text = render_text(
                snapshot,
                width=size.columns,
                height=None,
                auto_run=auto_run,
                fetch=fetch,
                offline=offline,
            )
            if follow and frames:
                print("\033[2J\033[H", end="")
            print(text, flush=True)
            frames += 1
            if not follow:
                return 0
            try:
                time.sleep(max(1.0, interval))
            except KeyboardInterrupt:
                return 0

    import curses

    try:
        return int(
            curses.wrapper(
                _curses_loop,
                cfg,
                interval,
                snapshot_interval,
                monitor_interval,
                auto_run,
                fetch,
                fetch_interval,
                write_monitor,
                offline,
            )
            or 0
        )
    except KeyboardInterrupt:
        return 0
