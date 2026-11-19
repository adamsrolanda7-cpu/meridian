"""Types partagés par les agents et le moteur.

Les objets sont volontairement simples et sérialisables : un desk doit pouvoir
rejouer une décision, l'auditer et la comparer à l'exécution réelle.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import pandas as pd


@dataclass
class Signal:
    asset: str
    sleeve: str
    score: float
    confidence: float
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Regime:
    label: str
    score: float
    confidence: float
    breadth: float
    vol_pct: float
    adx: float
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Target:
    asset: str
    weight: float
    score: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderIntent:
    """Intention de rééquilibrage, exprimée en poids cible.

    Le prix, la taille et le coût sont calculés au moment de l'exécution
    (ouverture suivante) pour éviter tout look-ahead.
    """

    asset: str
    target_weight: float
    reason: str = ""


@dataclass
class Order:
    asset: str
    side: str  # BUY / SELL
    notional: float
    qty: float
    price: float
    reason: str = ""
    target_weight: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fill:
    ts: pd.Timestamp
    asset: str
    side: str
    qty: float
    price: float
    notional: float
    fee: float
    slippage_bps: float
    reason: str = ""
    realized_pnl: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["ts"] = str(self.ts)
        return out


@dataclass
class Position:
    asset: str
    qty: float
    avg_price: float
    stop: float
    sleeve: str = ""
    entry_ts: pd.Timestamp | None = None
    entry_index: int = -1
    bars_held: int = 0
    last_price: float = 0.0
    high_water: float = 0.0

    @property
    def notional(self) -> float:
        return self.qty * self.last_price

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["entry_ts"] = str(self.entry_ts) if self.entry_ts is not None else None
        out["notional"] = self.notional
        out["unrealized_pnl"] = (self.last_price - self.avg_price) * self.qty
        return out


@dataclass
class Decision:
    """Trace d'une décision de fin de séance, pour l'audit et le memo."""

    ts: pd.Timestamp
    regime: Regime
    signals: list[Signal] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    orders: list[OrderIntent] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    gross_target: float = 0.0
    portfolio_vol: float = 0.0
    equity: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": str(self.ts),
            "regime": self.regime.as_dict(),
            "signals": [s.as_dict() for s in self.signals],
            "targets": [t.as_dict() for t in self.targets],
            "orders": [o.__dict__ for o in self.orders],
            "risk_notes": list(self.risk_notes),
            "gross_target": self.gross_target,
            "portfolio_vol": self.portfolio_vol,
            "equity": self.equity,
        }
