"""Compte réel Meridian : lecture du compte, plan d'ordres et exécution opt-in.

Le mode réel est volontairement impossible à activer par simple présence d'une
clé API. Il faut :

1. ``--mainnet`` (sinon le testnet est utilisé) ;
2. ``--execute`` pour envoyer les ordres ;
3. ``--confirm`` pour accepter explicitement le risque ;
4. ``MERIDIAN_LIVE_CONFIRM=YES`` dans l'environnement.

Sans ``--execute``, la commande reste un dry-run : elle lit le compte, calcule
les ordres et écrit le plan dans ``runs/live/plan.json``, sans rien envoyer.

Le module réutilise le moteur de décision Meridian pour obtenir les poids
cibles, puis compare ces cibles au compte réel. Les ventes passent avant les
achats, les plafonds de notionnel et de turnover sont appliqués, et aucune
position short n'est possible (spot long-only).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .backtest import Backtest
from .config import ROOT, get, load_config
from .data import ensure_data, load_panel, quality_report
from .env import env, env_bool, env_float, load_env
from .exchange import AccountSnapshot, CcxtExchange, ExchangeError
from .features import FeatureStore

LIVE_DIR = os.path.join(ROOT, "runs", "live")
LIVE_STATE_PATH = os.path.join(LIVE_DIR, "state.json")
LIVE_PLAN_PATH = os.path.join(LIVE_DIR, "plan.json")
LIVE_JOURNAL_PATH = os.path.join(LIVE_DIR, "journal.jsonl")


class LiveTradingError(RuntimeError):
    """Erreur de configuration ou de garde-fou du mode réel."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _append_jsonl(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


@dataclass
class LiveOrder:
    asset: str
    side: str
    qty: float
    notional: float
    price: float
    target_weight: float
    reason: str = ""
    status: str = "planned"
    order_id: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "side": self.side,
            "qty": self.qty,
            "notional": self.notional,
            "price": self.price,
            "target_weight": self.target_weight,
            "reason": self.reason,
            "status": self.status,
            "order_id": self.order_id,
            "error": self.error,
        }


@dataclass
class LiveState:
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    exchange_id: str = ""
    sandbox: bool = True
    quote: str = "USDT"
    peak_equity: float = 0.0
    last_equity: float = 0.0
    last_run: str = ""
    last_bar: str = ""
    turnover_date: str = ""
    turnover_usd: float = 0.0
    orders: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | None = None) -> "LiveState | None":
        path = path or LIVE_STATE_PATH
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(
            version=int(raw.get("version", 1)),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            exchange_id=raw.get("exchange_id", ""),
            sandbox=bool(raw.get("sandbox", True)),
            quote=raw.get("quote", "USDT"),
            peak_equity=float(raw.get("peak_equity", 0.0)),
            last_equity=float(raw.get("last_equity", 0.0)),
            last_run=raw.get("last_run", ""),
            last_bar=raw.get("last_bar", ""),
            turnover_date=raw.get("turnover_date", ""),
            turnover_usd=float(raw.get("turnover_usd", 0.0)),
            orders=list(raw.get("orders", [])),
            history=list(raw.get("history", [])),
        )

    @classmethod
    def new(cls, account: AccountSnapshot) -> "LiveState":
        now = _now()
        return cls(
            created_at=now,
            updated_at=now,
            exchange_id=account.exchange_id,
            sandbox=account.sandbox,
            quote=account.quote,
            peak_equity=account.equity,
            last_equity=account.equity,
        )

    def save(self, path: str | None = None) -> None:
        self.updated_at = _now()
        _write_json(path or LIVE_STATE_PATH, self.__dict__)


@dataclass
class SignalSnapshot:
    last_bar: str
    regime: str
    targets: dict[str, float]
    reasons: dict[str, str]
    stale_days: int
    source: str
    quality: dict
    portfolio_vol: float = 0.0
    risk_notes: list[str] = field(default_factory=list)


def exchange_settings(mainnet: bool = False, exchange_id: str | None = None) -> dict:
    """Lit les credentials depuis l'environnement, jamais depuis le dépôt."""
    load_env()
    return {
        "exchange_id": (exchange_id or env("MERIDIAN_EXCHANGE_ID", "binance")).lower(),
        "api_key": env("MERIDIAN_EXCHANGE_API_KEY"),
        "api_secret": env("MERIDIAN_EXCHANGE_API_SECRET"),
        "password": env("MERIDIAN_EXCHANGE_PASSPHRASE"),
        "sandbox": False if mainnet else env_bool("MERIDIAN_EXCHANGE_SANDBOX", True),
        "quote": env("MERIDIAN_QUOTE", "USDT").upper(),
    }


def risk_limits(
    max_order_usd: float | None = None,
    max_turnover_usd: float | None = None,
) -> tuple[float, float]:
    order = float(
        max_order_usd
        if max_order_usd is not None
        else env_float("MERIDIAN_MAX_ORDER_USD", 250.0)
    )
    turnover = float(
        max_turnover_usd
        if max_turnover_usd is not None
        else env_float("MERIDIAN_MAX_DAILY_TURNOVER_USD", 1_000.0)
    )
    if order <= 0:
        raise LiveTradingError("MERIDIAN_MAX_ORDER_USD doit être > 0")
    if turnover <= 0:
        raise LiveTradingError("MERIDIAN_MAX_DAILY_TURNOVER_USD doit être > 0")
    return order, turnover


def assert_execution_allowed(execute: bool, confirm: bool, settings: dict) -> None:
    """Vérifie les verrous avant tout envoi d'ordre."""
    if not execute:
        return
    if not confirm:
        raise LiveTradingError(
            "envoi réel refusé : ajoutez --confirm pour accepter explicitement le risque"
        )
    if env("MERIDIAN_LIVE_CONFIRM").upper() != "YES":
        raise LiveTradingError(
            "envoi réel refusé : définissez MERIDIAN_LIVE_CONFIRM=YES dans .env "
            "(ou dans l'environnement) après avoir relu la checklist"
        )
    if not settings.get("api_key") or not settings.get("api_secret"):
        raise LiveTradingError(
            "clés API absentes : renseignez MERIDIAN_EXCHANGE_API_KEY et "
            "MERIDIAN_EXCHANGE_API_SECRET dans .env"
        )


def make_exchange(settings: dict) -> CcxtExchange:
    return CcxtExchange(
        exchange_id=settings["exchange_id"],
        api_key=settings["api_key"],
        secret=settings["api_secret"],
        password=settings["password"],
        sandbox=settings["sandbox"],
        quote=settings["quote"],
    )


def align_panel(panel: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Aligne les actifs sur un calendrier commun.

    Le backtest historique utilise l'union des dates ; le live ne peut pas se
    permettre un ``.loc`` sur une date absente d'un actif. On prend donc
    l'intersection stricte, ce qui évite les trous et les dates orphelines.
    """
    if not panel:
        raise LiveTradingError("panel vide")
    common: pd.DatetimeIndex | None = None
    for frame in panel.values():
        common = frame.index if common is None else common.intersection(frame.index)
    if common is None or len(common) < 300:
        raise LiveTradingError("historique commun insuffisant pour calculer le signal")
    common = common.sort_values()
    return {symbol: frame.reindex(common) for symbol, frame in panel.items()}


def latest_signal(cfg: dict, refresh: bool = True) -> SignalSnapshot:
    """Calcule les poids cibles sur la dernière bougie clôturée."""
    if refresh:
        panel, source = ensure_data(cfg, quiet=True, allow_synthetic=False)
    else:
        panel, source = load_panel(cfg), "cache"
    quality = quality_report(cfg, panel)
    stale_days = max((int(q.get("stale_days", 0)) for q in quality.values()), default=0)
    max_stale = int(get(cfg, "monitor.max_stale_days", 3))
    if stale_days > max_stale:
        raise LiveTradingError(
            f"données trop anciennes : {stale_days} j > {max_stale} j. "
            "Lancez `python3 -m meridian download --force` avant de trader."
        )
    panel = align_panel(panel)
    try:
        store = FeatureStore.build(panel, cfg)
        result = Backtest(store, cfg).run()
    except Exception as exc:
        raise LiveTradingError(f"calcul du signal impossible: {exc}") from exc
    decision = result.latest_decision
    if decision is None:
        raise LiveTradingError("aucune décision disponible : historique insuffisant")
    targets = {t.asset: max(0.0, float(t.weight)) for t in decision.targets}
    reasons = {t.asset: t.reason for t in decision.targets}
    return SignalSnapshot(
        last_bar=result.end,
        regime=decision.regime.label,
        targets=targets,
        reasons=reasons,
        stale_days=stale_days,
        source=source,
        quality=quality,
        portfolio_vol=float(decision.portfolio_vol),
        risk_notes=list(decision.risk_notes),
    )


def clamp_targets(targets: dict[str, float], cfg: dict) -> dict[str, float]:
    """Applique les plafonds du mandat avant de comparer au compte réel."""
    max_asset = float(get(cfg, "strategy.max_asset_weight", 0.35))
    max_gross = float(get(cfg, "strategy.max_gross", 0.95))
    out = {asset: min(max(0.0, float(weight)), max_asset) for asset, weight in targets.items()}
    total = sum(out.values())
    if total > max_gross > 0:
        factor = max_gross / total
        out = {asset: weight * factor for asset, weight in out.items()}
    return out


def plan_orders(
    account: AccountSnapshot,
    targets: dict[str, float],
    cfg: dict,
    state: LiveState | None = None,
    max_order_usd: float = 250.0,
    max_turnover_usd: float = 1_000.0,
    reasons: dict[str, str] | None = None,
) -> tuple[list[LiveOrder], list[str]]:
    """Traduit les poids cibles en ordres, sans dépasser les garde-fous."""
    reasons = reasons or {}
    equity = float(account.equity)
    if equity <= 0:
        raise LiveTradingError("equity du compte réelle <= 0 : aucun ordre")
    quote = account.quote
    universe = list(cfg.get("universe", []))
    prices = account.prices
    current = {asset: account.weight(asset) for asset in universe}
    cash_free = float(account.free(quote))
    min_cash = float(get(cfg, "strategy.min_cash", 0.05)) * equity
    min_notional = float(get(cfg, "strategy.min_notional", 100.0))
    max_asset = float(get(cfg, "strategy.max_asset_weight", 0.35))
    max_gross = float(get(cfg, "strategy.max_gross", 0.95))
    fee_buffer = float(get(cfg, "costs.fee_bps", 8.0)) / 10_000.0
    dd_halt = float(get(cfg, "risk.dd_halt", 0.12))
    daily_limit = float(get(cfg, "risk.daily_loss_limit", 0.03))

    notes: list[str] = []
    peak = float(state.peak_equity) if state else 0.0
    last_equity = float(state.last_equity) if state else 0.0
    drawdown = equity / peak - 1.0 if peak > 0 else 0.0
    daily_return = equity / last_equity - 1.0 if last_equity > 0 else 0.0
    block_buys = False
    if drawdown <= -dd_halt:
        block_buys = True
        notes.append(f"drawdown réel {drawdown:.1%} ≤ -{dd_halt:.0%} : achats bloqués")
    if daily_return <= -daily_limit:
        block_buys = True
        notes.append(f"perte journalière {daily_return:.1%} ≤ -{daily_limit:.0%} : achats bloqués")

    targets = clamp_targets(targets, cfg)
    gross_target = sum(targets.values())
    if gross_target > max_gross + 1e-9:
        notes.append(f"gross cible {gross_target:.1%} > plafond {max_gross:.0%}")

    today = _today()
    used = float(state.turnover_usd) if state and state.turnover_date == today else 0.0
    remaining = max(0.0, float(max_turnover_usd) - used)
    if remaining <= 0:
        notes.append("plafond de turnover journalier déjà atteint : aucun ordre")
        return [], notes

    orders: list[LiveOrder] = []
    planned = 0.0

    # 1) Ventes d'abord : elles libèrent du cash et réduisent le risque.
    for asset in sorted(universe, key=lambda a: targets.get(a, 0.0) - current.get(a, 0.0)):
        target = targets.get(asset, 0.0)
        cur = current.get(asset, 0.0)
        delta = target - cur
        if delta >= -1e-9:
            continue
        price = prices.get(asset)
        if price is None or price <= 0:
            notes.append(f"{asset}: prix indisponible, vente ignorée")
            continue
        desired = (cur - target) * equity
        if desired < min_notional:
            notes.append(f"{asset}: vente {desired:.0f} $ sous le minimum {min_notional:.0f} $")
            continue
        free_qty = account.free(asset)
        if free_qty <= 0:
            notes.append(f"{asset}: aucune quantité libre à vendre")
            continue
        qty = min(free_qty, desired / price)
        notional = qty * price
        if notional > max_order_usd:
            qty = max_order_usd / price
            notional = qty * price
            notes.append(f"{asset}: vente plafonnée à {max_order_usd:.0f} $")
        if notional > remaining:
            qty = remaining / price
            notional = qty * price
            notes.append(f"{asset}: vente limitée par le turnover restant")
        if notional < min_notional or qty <= 0:
            continue
        orders.append(
            LiveOrder(
                asset=asset,
                side="SELL",
                qty=qty,
                notional=notional,
                price=price,
                target_weight=target,
                reason=reasons.get(asset, "rééquilibrage"),
            )
        )
        cash_free += notional * (1.0 - fee_buffer)
        remaining -= notional
        planned += notional

    # 2) Achats, dans l'ordre de conviction, dans la limite du cash et du turnover.
    if block_buys:
        notes.append("achats bloqués par le gouverneur de risque réel")
    else:
        buy_candidates = []
        for asset in universe:
            target = targets.get(asset, 0.0)
            cur = current.get(asset, 0.0)
            delta = target - cur
            if delta <= 1e-9:
                continue
            price = prices.get(asset)
            if price is None or price <= 0:
                notes.append(f"{asset}: prix indisponible, achat ignoré")
                continue
            buy_candidates.append((delta, asset, target, cur, price))
        for delta, asset, target, cur, price in sorted(buy_candidates, reverse=True):
            desired = delta * equity
            room = max(0.0, (max_asset - cur) * equity)
            desired = min(desired, room)
            budget = max(0.0, cash_free - min_cash)
            notional = min(desired, budget / (1.0 + fee_buffer), max_order_usd, remaining)
            if notional < min_notional:
                notes.append(f"{asset}: achat {notional:.0f} $ sous le minimum {min_notional:.0f} $")
                continue
            qty = notional / price
            orders.append(
                LiveOrder(
                    asset=asset,
                    side="BUY",
                    qty=qty,
                    notional=notional,
                    price=price,
                    target_weight=target,
                    reason=reasons.get(asset, "rééquilibrage"),
                )
            )
            cash_free -= notional
            remaining -= notional
            planned += notional

    if planned > max_turnover_usd + 1e-9:
        raise LiveTradingError("bug de planification : turnover supérieur au plafond")
    if not orders:
        notes.append("aucun ordre : portefeuille déjà dans la bande morte")
    return orders, notes


def format_account(account: AccountSnapshot) -> str:
    mode = "TESTNET" if account.sandbox else "MAINNET"
    lines = [
        "=" * 72,
        f"MERIDIAN COMPTE RÉEL — {account.exchange_id.upper()} · {mode} · LECTURE SEULE",
        "=" * 72,
        f"Equity            : {account.equity:,.2f} {account.quote}".replace(",", " "),
        f"Cash libre        : {account.free(account.quote):,.2f} {account.quote}".replace(",", " "),
        f"Prix récupérés    : {len(account.prices) - 1} actif(s)",
        f"Horodatage        : {account.fetched_at}",
        "",
        "Positions :",
    ]
    shown = False
    for asset, balance in sorted(account.balances.items()):
        if asset == account.quote:
            continue
        price = account.prices.get(asset, 0.0)
        if balance.total <= 0 and balance.free <= 0:
            continue
        shown = True
        weight = account.weight(asset)
        lines.append(
            f"  {asset:<9} libre {balance.free:>14.8f} · total {balance.total:>14.8f} · "
            f"{price:>12,.2f} {account.quote} · {weight * 100:5.1f}%"
        )
    if not shown:
        lines.append("  aucune position hors stablecoin")
    lines.append("")
    if account.sandbox:
        lines.append("Mode testnet : aucun argent réel n'est engagé.")
    else:
        lines.append("! Mode mainnet : vérifiez les permissions de la clé (aucun retrait).")
    lines.append("Aucun ordre n'a été envoyé par cette commande.")
    return "\n".join(lines)


def format_plan(
    account: AccountSnapshot,
    signal: SignalSnapshot,
    orders: list[LiveOrder],
    notes: list[str],
    execute: bool,
) -> str:
    mode = "TESTNET" if account.sandbox else "MAINNET"
    lines = [
        "=" * 78,
        f"MERIDIAN LIVE — plan de rééquilibrage · {mode}",
        "=" * 78,
        f"Dernière bougie   : {signal.last_bar} · régime {signal.regime}",
        f"Source données    : {signal.source} · retard max {signal.stale_days} j",
        f"Equity compte     : {account.equity:,.2f} {account.quote}".replace(",", " "),
        f"Cash libre        : {account.free(account.quote):,.2f} {account.quote}".replace(",", " "),
        f"Vol ex-ante       : {signal.portfolio_vol * 100:.1f}%",
        "",
        f"{'Actif':<9} {'Actuel':>8} {'Cible':>8} {'Delta':>8} {'Ordre':>6} "
        f"{'Qté':>14} {'Notionnel':>12}",
    ]
    for order in orders:
        cur = account.weight(order.asset)
        delta = order.target_weight - cur
        lines.append(
            f"{order.asset:<9} {cur * 100:7.1f}% {order.target_weight * 100:7.1f}% "
            f"{delta * 100:+7.1f}% {order.side:>6} {order.qty:>14.8f} "
            f"{order.notional:>10,.0f} $".replace(",", " ")
        )
    if not orders:
        lines.append("  aucun ordre à envoyer")
    if notes:
        lines.append("")
        lines.append("Notes :")
        for note in notes:
            lines.append(f"  - {note}")
    lines.append("")
    if execute:
        lines.append("Mode EXECUTION : les ordres vont être envoyés à l'exchange.")
    else:
        lines.append("Mode DRY-RUN : aucun ordre réel n'a été envoyé.")
    return "\n".join(lines)


def run_account(
    cfg: dict | None = None,
    mainnet: bool = False,
    exchange_id: str | None = None,
    json_output: bool = False,
) -> dict:
    cfg = cfg or load_config()
    settings = exchange_settings(mainnet=mainnet, exchange_id=exchange_id)
    exchange = make_exchange(settings)
    try:
        account = exchange.fetch_account(list(cfg.get("universe", [])))
    finally:
        exchange.close()
    if json_output:
        print(json.dumps(account.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_account(account))
    return account.as_dict()


def run_trade(
    cfg: dict | None = None,
    execute: bool = False,
    confirm: bool = False,
    mainnet: bool = False,
    exchange_id: str | None = None,
    order_type: str = "market",
    max_order_usd: float | None = None,
    max_turnover_usd: float | None = None,
    refresh: bool = True,
    json_output: bool = False,
) -> dict:
    cfg = cfg or load_config()
    settings = exchange_settings(mainnet=mainnet, exchange_id=exchange_id)
    assert_execution_allowed(execute, confirm, settings)
    order_limit, turnover_limit = risk_limits(max_order_usd, max_turnover_usd)
    if order_type not in ("market", "limit"):
        raise LiveTradingError("--order-type doit valoir market ou limit")
    if execute and not settings["sandbox"] and not mainnet:
        raise LiveTradingError(
            "compte mainnet détecté : ajoutez --mainnet pour confirmer explicitement"
        )

    # Le signal est calculé avant toute connexion : des données périmées
    # doivent arrêter le processus sans même ouvrir la session exchange.
    signal = latest_signal(cfg, refresh=refresh)

    exchange = make_exchange(settings)
    try:
        account = exchange.fetch_account(list(cfg.get("universe", [])))
        state = LiveState.load() or LiveState.new(account)
        if state.exchange_id and state.exchange_id != account.exchange_id:
            raise LiveTradingError(
                f"l'état live appartient à {state.exchange_id}, pas à {account.exchange_id}"
            )
        if state.created_at and state.sandbox != account.sandbox:
            raise LiveTradingError(
                "l'état live a été créé sur un autre environnement "
                f"({'testnet' if state.sandbox else 'mainnet'} vs "
                f"{'testnet' if account.sandbox else 'mainnet'}). "
                "Archivez runs/live/state.json avant de changer d'environnement."
            )
        orders, notes = plan_orders(
            account,
            signal.targets,
            cfg,
            state=state,
            max_order_usd=order_limit,
            max_turnover_usd=turnover_limit,
            reasons=signal.reasons,
        )
        plan = {
            "generated_at": _now(),
            "execute": execute,
            "exchange": account.exchange_id,
            "sandbox": account.sandbox,
            "quote": account.quote,
            "last_bar": signal.last_bar,
            "regime": signal.regime,
            "stale_days": signal.stale_days,
            "source": signal.source,
            "equity": account.equity,
            "cash": account.free(account.quote),
            "targets": signal.targets,
            "orders": [o.as_dict() for o in orders],
            "notes": notes,
            "limits": {"max_order_usd": order_limit, "max_turnover_usd": turnover_limit},
        }
        _write_json(LIVE_PLAN_PATH, plan)

        if not json_output:
            print(format_plan(account, signal, orders, notes, execute))

        if not execute:
            return plan

        if not orders:
            return plan

        sent = 0
        for order in orders:
            try:
                minimum = exchange.min_notional(order.asset, order.price)
            except Exception:
                minimum = 0.0
            if order.notional < minimum:
                order.status = "skipped"
                order.error = (
                    f"notionnel {order.notional:.2f} $ < minimum exchange {minimum:.2f} $"
                )
                notes.append(f"{order.asset}: {order.error}")
                continue
            try:
                raw = exchange.create_order(
                    order.asset,
                    order.side,
                    order.qty,
                    order_type=order_type,
                    price=order.price if order_type == "limit" else None,
                )
                order.status = "sent"
                order.order_id = str(raw.get("id", ""))
                sent += 1
            except ExchangeError as exc:
                order.status = "error"
                order.error = str(exc)
                notes.append(f"{order.asset}: {exc}")
                break

        try:
            after = exchange.fetch_account(list(cfg.get("universe", [])))
        except ExchangeError as exc:
            after = account
            notes.append(f"relecture du compte impossible après exécution: {exc}")
        sent_notional = sum(o.notional for o in orders if o.status == "sent")
        today = _today()
        previous_turnover = state.turnover_usd if state.turnover_date == today else 0.0
        state.exchange_id = after.exchange_id
        state.sandbox = after.sandbox
        state.quote = after.quote
        state.peak_equity = max(state.peak_equity, after.equity)
        state.last_equity = after.equity
        state.last_run = _now()
        state.last_bar = signal.last_bar
        state.turnover_date = today
        state.turnover_usd = previous_turnover + sent_notional
        state.orders = [o.as_dict() for o in orders]
        entry = {
            "run_at": state.last_run,
            "exchange": after.exchange_id,
            "sandbox": after.sandbox,
            "equity_before": account.equity,
            "equity_after": after.equity,
            "last_bar": signal.last_bar,
            "orders_sent": sent,
            "orders": [o.as_dict() for o in orders],
            "notes": notes,
        }
        state.history = (state.history + [entry])[-200:]
        state.save()
        _append_jsonl(LIVE_JOURNAL_PATH, entry)

        result = {
            **plan,
            "executed": True,
            "orders": [o.as_dict() for o in orders],
            "orders_sent": sent,
            "equity_after": after.equity,
            "notes": notes,
        }
        _write_json(LIVE_PLAN_PATH, result)
        if not json_output:
            print("")
            print(f"Ordres envoyés : {sent}/{len(orders)} · equity après : {after.equity:,.2f} $".replace(",", " "))
            print(f"Journal : {LIVE_JOURNAL_PATH}")
            print("Vérifiez les ordres ouverts et les soldes sur l'exchange.")
        return result
    finally:
        exchange.close()


def status() -> dict:
    """État local du mode réel, sans connexion à l'exchange."""
    load_env()
    state = LiveState.load()
    settings = exchange_settings()
    return {
        "env_file": os.path.exists(os.path.join(ROOT, ".env")),
        "exchange_id": settings["exchange_id"],
        "sandbox": settings["sandbox"],
        "credentials_present": bool(settings["api_key"] and settings["api_secret"]),
        "confirm": env("MERIDIAN_LIVE_CONFIRM").upper() == "YES",
        "max_order_usd": env_float("MERIDIAN_MAX_ORDER_USD", 250.0),
        "max_turnover_usd": env_float("MERIDIAN_MAX_DAILY_TURNOVER_USD", 1_000.0),
        "state": state.__dict__ if state else None,
    }


def format_status(payload: dict) -> str:
    mode = "TESTNET" if payload["sandbox"] else "MAINNET"
    lines = [
        "MERIDIAN — préparation du mode réel",
        "=" * 50,
        f"Fichier .env          : {'oui' if payload['env_file'] else 'non'}",
        f"Exchange              : {payload['exchange_id']} · {mode}",
        f"Clés API              : {'présentes' if payload['credentials_present'] else 'ABSENTES'}",
        f"MERIDIAN_LIVE_CONFIRM : {'YES' if payload['confirm'] else 'NO'}",
        f"Plafond par ordre     : {payload['max_order_usd']:,.0f} $".replace(",", " "),
        f"Plafond turnover/jour : {payload['max_turnover_usd']:,.0f} $".replace(",", " "),
    ]
    state = payload.get("state")
    if state:
        lines.append("")
        lines.append(
            f"État live             : {state.get('exchange_id')} · "
            f"equity {state.get('last_equity', 0):,.2f} $ · "
            f"dernier run {state.get('last_run') or '—'}".replace(",", " ")
        )
        lines.append(f"Dernière bougie       : {state.get('last_bar') or '—'}")
    else:
        lines.append("")
        lines.append("État live : aucun run réel enregistré.")
    lines.append("")
    lines.append("Aucun ordre n'a été envoyé par cette commande.")
    return "\n".join(lines)
