"""Comptabilité du portefeuille (spot long-only).

Le Book est la source de vérité : cash, positions, stops, PnL réalisé, frais.
Il est utilisé à l'identique par le backtest et par le paper trading, ce qui
permet de comparer les décisions à l'exécution.
"""
from __future__ import annotations

import math
from typing import Iterable

import pandas as pd

from .types import Fill, Position


class Book:
    def __init__(self, cash: float):
        self.initial_cash = float(cash)
        self.cash = float(cash)
        self.positions: dict[str, Position] = {}
        self.trades: list[dict] = []
        self.fees_paid = 0.0
        self.realized_pnl = 0.0

    # ------------------------------------------------------------------ marks
    def mark(self, prices: dict[str, float]) -> None:
        for asset, pos in self.positions.items():
            price = prices.get(asset)
            if price is not None and math.isfinite(price) and price > 0:
                pos.last_price = float(price)
                pos.high_water = max(pos.high_water, pos.last_price)

    def equity(self, prices: dict[str, float] | None = None) -> float:
        if prices:
            self.mark(prices)
        return float(self.cash + sum(p.notional for p in self.positions.values()))

    def weights(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        equity = self.equity(prices)
        if equity <= 0:
            return {a: 0.0 for a in self.positions}
        return {a: p.notional / equity for a, p in self.positions.items()}

    def gross_exposure(self, prices: dict[str, float] | None = None) -> float:
        return float(sum(self.weights(prices).values()))

    # --------------------------------------------------------------- exécution
    def buy(
        self,
        ts: pd.Timestamp,
        asset: str,
        qty: float,
        price: float,
        fee: float,
        reason: str = "",
        stop: float | None = None,
        sleeve: str = "",
        entry_index: int = -1,
    ) -> Fill:
        qty = float(qty)
        if qty <= 0 or price <= 0:
            raise ValueError("quantité/prix invalides")
        notional = qty * price
        if notional + fee > self.cash + 1e-6:
            raise ValueError(
                f"cash insuffisant: besoin {notional + fee:.2f}, dispo {self.cash:.2f}"
            )
        self.cash -= notional + fee
        self.fees_paid += fee
        pos = self.positions.get(asset)
        if pos is None:
            pos = Position(
                asset=asset,
                qty=qty,
                avg_price=price,
                stop=float(stop if stop is not None else price * 0.85),
                sleeve=sleeve,
                entry_ts=ts,
                entry_index=entry_index,
                bars_held=0,
                last_price=price,
                high_water=price,
            )
            self.positions[asset] = pos
        else:
            total_qty = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + price * qty) / total_qty
            pos.qty = total_qty
            pos.last_price = price
            if stop is not None:
                pos.stop = max(pos.stop, float(stop))
        fill = Fill(ts, asset, "BUY", qty, price, notional, fee, 0.0, reason)
        self.trades.append({**fill.as_dict(), "sleeve": pos.sleeve, "pnl": 0.0})
        return fill

    def sell(
        self,
        ts: pd.Timestamp,
        asset: str,
        qty: float,
        price: float,
        fee: float,
        reason: str = "",
        slippage_bps: float = 0.0,
    ) -> Fill:
        pos = self.positions.get(asset)
        if pos is None:
            raise ValueError(f"aucune position {asset}")
        qty = min(float(qty), pos.qty)
        if qty <= 0:
            raise ValueError("quantité invalide")
        notional = qty * price
        pnl = (price - pos.avg_price) * qty - fee
        self.cash += notional - fee
        self.fees_paid += fee
        self.realized_pnl += pnl
        pos.qty -= qty
        pos.last_price = price
        if pos.qty <= 1e-9:
            self.positions.pop(asset, None)
        fill = Fill(ts, asset, "SELL", qty, price, notional, fee, slippage_bps, reason, pnl)
        self.trades.append({**fill.as_dict(), "sleeve": pos.sleeve, "pnl": pnl})
        return fill

    # ---------------------------------------------------------------- reporting
    def unrealized(self) -> float:
        return float(sum((p.last_price - p.avg_price) * p.qty for p in self.positions.values()))

    def snapshot(self) -> dict:
        return {
            "cash": self.cash,
            "equity": self.equity(),
            "initial_cash": self.initial_cash,
            "positions": {a: p.as_dict() for a, p in self.positions.items()},
            "fees_paid": self.fees_paid,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized(),
            "gross_exposure": self.gross_exposure(),
        }

    def to_dict(self) -> dict:
        return {
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "fees_paid": self.fees_paid,
            "realized_pnl": self.realized_pnl,
            "positions": {
                a: {
                    "asset": p.asset,
                    "qty": p.qty,
                    "avg_price": p.avg_price,
                    "stop": p.stop,
                    "sleeve": p.sleeve,
                    "entry_ts": str(p.entry_ts) if p.entry_ts is not None else None,
                    "entry_index": p.entry_index,
                    "bars_held": p.bars_held,
                    "last_price": p.last_price,
                    "high_water": p.high_water,
                }
                for a, p in self.positions.items()
            },
            "trades": self.trades,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Book":
        book = cls(float(data.get("initial_cash", data.get("cash", 0.0))))
        book.cash = float(data["cash"])
        book.fees_paid = float(data.get("fees_paid", 0.0))
        book.realized_pnl = float(data.get("realized_pnl", 0.0))
        for asset, raw in data.get("positions", {}).items():
            book.positions[asset] = Position(
                asset=asset,
                qty=float(raw["qty"]),
                avg_price=float(raw["avg_price"]),
                stop=float(raw["stop"]),
                sleeve=raw.get("sleeve", ""),
                entry_ts=pd.Timestamp(raw["entry_ts"]) if raw.get("entry_ts") else None,
                entry_index=int(raw.get("entry_index", -1)),
                bars_held=int(raw.get("bars_held", 0)),
                last_price=float(raw.get("last_price", raw["avg_price"])),
                high_water=float(raw.get("high_water", raw["avg_price"])),
            )
        book.trades = list(data.get("trades", []))
        return book


def weights_from_targets(targets: Iterable) -> dict[str, float]:
    return {t.asset: max(0.0, float(t.weight)) for t in targets}
