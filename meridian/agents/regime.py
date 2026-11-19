"""Agent de régime : lit l'état macro du marché et le traduit en allocation.

Le régime est calculé sur BTC (ancre beta) + breadth des quatre actifs. Il
n'est pas un signal d'achat : c'est un interrupteur de risque qui décide quels
sleeves ont le droit de s'exprimer.
"""
from __future__ import annotations

import pandas as pd

from ..features import FeatureStore
from ..types import Regime
from .base import Agent


class RegimeAgent(Agent):
    name = "regime"
    role = "Classer le marché en TREND_UP / RANGE / TREND_DOWN / CRISIS et pondérer les sleeves."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.frame: pd.DataFrame = pd.DataFrame()

    def compute(self, store: FeatureStore) -> pd.DataFrame:
        self.frame = store.regime
        return self.frame

    def at(self, date: pd.Timestamp) -> Regime:
        row = self.frame.loc[date]
        return Regime(
            label=str(row["label"]),
            score=float(row["score"]) if pd.notna(row["score"]) else 0.0,
            confidence=float(row["confidence"]) if pd.notna(row["confidence"]) else 0.0,
            breadth=float(row["breadth"]) if pd.notna(row["breadth"]) else 0.0,
            vol_pct=float(row["vol_pct"]) if pd.notna(row["vol_pct"]) else 0.0,
            adx=float(row["btc_adx"]) if pd.notna(row["btc_adx"]) else 0.0,
            reason=str(row["reason"]),
        )
