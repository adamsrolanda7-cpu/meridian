"""Agent d'exécution : traduit des poids cibles en ordres, puis en fills.

Hypothèses explicites (backtest et paper) :
- décision sur la clôture t, exécution à l'ouverture t+1 ;
- ventes avant achats, pas de levier, cash minimum conservé ;
- frais taker + slippage + impact croissant avec la taille relative à l'ADV ;
- plafond de participation pour ne pas simuler des ordres irréalistes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import get
from ..portfolio import Book
from ..types import Fill, Order, OrderIntent
from .base import Agent


@dataclass
class ExecutionReport:
    fills: list[Fill] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class ExecutionAgent(Agent):
    name = "execution"
    role = "Exécuter au moindre coût, sans levier, avec contrôle de liquidité et de cash."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.fee_bps = float(get(cfg, "costs.fee_bps", 8.0))
        self.slip_bps = float(get(cfg, "costs.slippage_bps", 5.0))
        self.impact_bps = float(get(cfg, "costs.impact_coef_bps", 20.0))
        self.min_notional = float(get(cfg, "strategy.min_notional", 100.0))
        self.min_cash = float(get(cfg, "strategy.min_cash", 0.05))
        self.max_adv_pct = float(get(cfg, "risk.max_adv_pct", 0.05))

    def _impact(self, notional: float, adv: float) -> float:
        impact = 0.0
        if adv and np.isfinite(adv) and adv > 0 and notional > 0:
            ratio = (notional / adv) / 0.01
            impact = self.impact_bps * math.sqrt(max(0.0, ratio))
            impact = min(impact, 150.0)
        return impact

    def execute(
        self,
        intents: list[OrderIntent],
        book: Book,
        prices: dict[str, float],
        adv: dict[str, float],
        ts: pd.Timestamp,
        bar_index: int,
        stops: dict[str, float] | None = None,
        sleeves: dict[str, str] | None = None,
    ) -> ExecutionReport:
        stops = stops or {}
        sleeves = sleeves or {}
        report = ExecutionReport()
        equity = book.equity(prices)
        if equity <= 0:
            report.notes.append("equity nulle: aucun ordre")
            return report
        current = book.weights(prices)
        fee_rate = self.fee_bps / 10_000.0
        targets = {i.asset: max(0.0, i.target_weight) for i in intents}
        reasons = {i.asset: i.reason for i in intents}

        # 1) Ventes : libèrent du cash et réduisent le risque en premier.
        for intent in sorted(intents, key=lambda x: x.target_weight):
            asset = intent.asset
            pos = book.positions.get(asset)
            if pos is None or pos.qty <= 0:
                continue
            target = targets.get(asset, 0.0)
            cur = current.get(asset, 0.0)
            if target >= cur - 1e-6:
                continue
            price = prices.get(asset)
            if price is None or not np.isfinite(price) or price <= 0:
                report.notes.append(f"{asset}: prix vendeur indisponible")
                continue
            if target <= 1e-9:
                qty = pos.qty
                target = 0.0
            else:
                qty = min(pos.qty, (cur - target) * equity / price)
            adv_a = adv.get(asset, float("nan"))
            if adv_a and np.isfinite(adv_a) and adv_a > 0:
                qty = min(qty, self.max_adv_pct * adv_a / price)
            notional = qty * price
            if notional < self.min_notional and target > 1e-9:
                report.notes.append(f"{asset}: vente sous le notionnel minimum")
                continue
            if qty <= 1e-12:
                continue
            held_before = pos.qty
            slip = self.slip_bps + self._impact(notional, adv_a)
            fill_price = price * (1.0 - slip / 10_000.0)
            fee = notional * fee_rate
            fill = book.sell(ts, asset, qty, fill_price, fee, reasons.get(asset, "rebalance"), slip)
            report.fills.append(fill)
            report.orders.append(
                Order(asset, "SELL", notional, qty, fill_price, reasons.get(asset, ""), target)
            )
            if qty < held_before - 1e-12:
                report.notes.append(f"{asset}: vente partielle (liquidité/arrondi)")

        # 2) Achats, dans l'ordre de conviction (poids cible décroissant).
        budget = max(0.0, book.cash - self.min_cash * equity)
        buy_intents = []
        for intent in intents:
            asset = intent.asset
            target = targets.get(asset, 0.0)
            cur = book.weights(prices).get(asset, 0.0)
            if target <= cur + 1e-6:
                continue
            price = prices.get(asset)
            if price is None or not np.isfinite(price) or price <= 0:
                report.notes.append(f"{asset}: prix acheteur indisponible")
                continue
            buy_intents.append((target - cur, intent))
        buy_intents.sort(key=lambda pair: pair[0], reverse=True)

        for delta_weight, intent in buy_intents:
            asset = intent.asset
            price = prices[asset]
            desired_notional = delta_weight * equity
            adv_a = adv.get(asset, float("nan"))
            max_notional = self.max_adv_pct * adv_a if adv_a and np.isfinite(adv_a) and adv_a > 0 else desired_notional
            notional = min(desired_notional, max_notional, budget)
            if notional < self.min_notional:
                report.notes.append(f"{asset}: achat sous le notionnel minimum")
                continue
            slip = self.slip_bps + self._impact(notional, adv_a)
            fill_price = price * (1.0 + slip / 10_000.0)
            qty = notional / fill_price
            fee = qty * fill_price * fee_rate
            # Le budget doit couvrir le notionnel exécuté + les frais, y
            # compris le slippage (sinon le cash peut passer sous le plancher).
            if qty * fill_price + fee > budget + 1e-9:
                qty = max(0.0, budget / (fill_price * (1.0 + fee_rate)))
                notional = qty * fill_price
                fee = qty * fill_price * fee_rate
                if notional < self.min_notional:
                    continue
            stop = stops.get(asset)
            fill = book.buy(
                ts,
                asset,
                qty,
                fill_price,
                fee,
                reasons.get(asset, "rebalance"),
                stop=stop,
                sleeve=sleeves.get(asset, ""),
                entry_index=bar_index,
            )
            budget -= qty * fill_price + fee
            report.fills.append(fill)
            report.orders.append(
                Order(asset, "BUY", notional, qty, fill_price, reasons.get(asset, ""), targets.get(asset, 0.0))
            )

        if not report.fills:
            report.notes.append("aucun fill: portefeuille déjà en cible ou contraintes")
        return report

    def liquidate(
        self,
        book: Book,
        prices: dict[str, float],
        adv: dict[str, float],
        ts: pd.Timestamp,
        reason: str = "liquidation",
    ) -> ExecutionReport:
        report = ExecutionReport()
        for asset in list(book.positions):
            pos = book.positions[asset]
            price = prices.get(asset)
            if price is None or not np.isfinite(price) or price <= 0:
                continue
            adv_a = adv.get(asset, float("nan"))
            qty = pos.qty
            if adv_a and np.isfinite(adv_a) and adv_a > 0:
                qty = min(qty, self.max_adv_pct * adv_a / price)
            if qty <= 0:
                continue
            notional = qty * price
            slip = self.slip_bps + self._impact(notional, adv_a)
            fill_price = price * (1.0 - slip / 10_000.0)
            fee = notional * self.fee_bps / 10_000.0
            fill = book.sell(ts, asset, qty, fill_price, fee, reason, slip)
            report.fills.append(fill)
        return report
