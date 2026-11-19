"""Agents de signal : tendance, retour à la moyenne, momentum transversal.

Chaque agent produit un score borné [0, 1] (long-only) et une confiance
[0, 1]. Les formules sont vectorisées et causales ; l'orchestrateur les
combine ensuite selon le régime.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get
from ..features import FeatureStore
from ..types import Signal
from .base import Agent


def _clip01(value: float) -> float:
    if value is None or not np.isfinite(value):
        return 0.0
    return float(np.clip(value, 0.0, 1.0))


class TrendAgent(Agent):
    name = "trend"
    role = "Capturer la persistance de tendance moyen terme via TSMOM, empilement EMA et cassure Donchian."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.win = int(get(cfg, "strategy.trend.mom_win", 90))
        self.frames: dict[str, pd.DataFrame] = {}

    def compute(self, store: FeatureStore) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for symbol, frame in store.frames.items():
            risk_mom = frame["risk_mom"]
            stack = frame["trend_stack"]
            breakout = frame["breakout"]
            raw = (
                0.50 * np.tanh(risk_mom / 1.2)
                + 0.30 * stack.fillna(0.0)
                + 0.20 * breakout.fillna(0.0)
            )
            # Long-only : on ne prend une exposition de tendance que si le
            # prix est au-dessus de la moyenne longue. Sous EMA200, le sleeve
            # tendance est plat ; le sleeve mean-reversion peut travailler.
            eligible = (frame["close"] > frame["ema_slow"]) & frame["ema_slow"].notna()
            score = raw.where(eligible, 0.0).clip(lower=0.0, upper=1.0)

            agreement = (
                0.45 * (stack > 0).astype(float)
                + 0.30 * breakout.fillna(0.0)
                + 0.25 * (risk_mom > 0).astype(float)
            )
            confidence = (
                0.35 * (frame["adx"] / 35.0).clip(0.0, 1.0).fillna(0.0)
                + 0.35 * (risk_mom.abs() / 1.5).clip(0.0, 1.0).fillna(0.0)
                + 0.30 * agreement
            ).clip(0.0, 1.0)

            out[symbol] = pd.DataFrame({"score": score, "confidence": confidence}).reindex(store.dates)
            out[symbol]["score"] = out[symbol]["score"].fillna(0.0)
            out[symbol]["confidence"] = out[symbol]["confidence"].fillna(0.0)
        self.frames = out
        return out

    def signal(self, symbol: str, date: pd.Timestamp) -> Signal:
        frame = self.frames[symbol]
        row = frame.loc[date]
        feat = self._feature_row(symbol, date)
        score = _clip01(row["score"])
        conf = _clip01(row["confidence"])
        if score <= 0:
            reason = "sous EMA200 ou momentum insuffisant"
        else:
            reason = (
                f"TSMOM {feat.get('risk_mom', 0):+.2f}, "
                f"stack {feat.get('trend_stack', 0):+.0f}, "
                f"cassure {int(feat.get('breakout', 0) or 0)}, ADX {feat.get('adx', 0):.0f}"
            )
        return Signal(symbol, self.name, score, conf, reason)

    def _feature_row(self, symbol: str, date: pd.Timestamp) -> pd.Series:
        # Le backtest attache le FeatureStore courant pour les raisons.
        store = getattr(self, "_store", None)
        if store is None:
            return pd.Series(dtype=float)
        return store.frames[symbol].loc[date]

    def attach(self, store: FeatureStore) -> None:
        self._store = store


class MeanReversionAgent(Agent):
    name = "meanrev"
    role = "Acheter les excès baissiers en régime RANGE, avec sortie rapide et stop serré."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.z_entry = float(get(cfg, "strategy.meanrev.z_entry", 1.8))
        self.rsi_entry = float(get(cfg, "strategy.meanrev.rsi_entry", 38))
        self.exit_z = float(get(cfg, "strategy.meanrev.exit_z", 0.25))
        self.max_hold = int(get(cfg, "strategy.meanrev.max_hold", 10))
        self.market_breadth_min = float(get(cfg, "strategy.meanrev.market_breadth_min", 0.0))
        self.asset_above_slow = bool(get(cfg, "strategy.meanrev.asset_above_slow", False))
        self.enabled = bool(get(cfg, "strategy.meanrev.enabled", True))
        self.frames: dict[str, pd.DataFrame] = {}
        self._store: FeatureStore | None = None

    def compute(self, store: FeatureStore) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        breadth = store.regime["breadth"].reindex(store.dates) if "breadth" in store.regime else pd.Series(1.0, index=store.dates)
        for symbol, frame in store.frames.items():
            # Filtre anti-couteau : on évite d'acheter un actif qui s'effondre
            # très loin de sa moyenne longue.
            not_collapsing = frame["close"] > frame["ema_slow"] * 0.75
            if self.asset_above_slow:
                not_collapsing = frame["close"] > frame["ema_slow"]
            # Filtre de marché : ne pas acheter des dips quand la largeur du
            # marché est trop faible, même si le régime est classé RANGE.
            market_ok = pd.Series(True, index=store.dates)
            if self.market_breadth_min > 0:
                market_ok = breadth.fillna(0.0) >= self.market_breadth_min
            setup = (
                self.enabled
                & (frame["z20"] <= -self.z_entry)
                & (frame["rsi"] <= self.rsi_entry)
                & not_collapsing
                & frame["vol30"].lt(1.0)
                & market_ok.reindex(store.dates).fillna(False)
            )
            score = ((-frame["z20"] - 0.8) / 2.0).clip(0.0, 1.0).where(setup, 0.0)
            confidence = (
                0.50 + 0.50 * ((frame["z20"].abs() - self.z_entry) / 1.5).clip(0.0, 1.0)
            ).where(setup, 0.0)
            out[symbol] = pd.DataFrame({"score": score.fillna(0.0), "confidence": confidence.fillna(0.0)}).reindex(store.dates)
            out[symbol] = out[symbol].fillna(0.0)
        self.frames = out
        self._store = store
        return out

    def should_exit(self, symbol: str, date: pd.Timestamp, bars_held: int) -> bool:
        if self._store is None:
            return False
        row = self._store.frames[symbol].loc[date]
        if not np.isfinite(row.get("z20", np.nan)):
            return False
        return bool(
            row["z20"] >= self.exit_z
            or row["rsi"] >= 55.0
            or bars_held >= self.max_hold
        )

    def signal(self, symbol: str, date: pd.Timestamp) -> Signal:
        row = self.frames[symbol].loc[date]
        feat = self._store.frames[symbol].loc[date] if self._store else pd.Series(dtype=float)
        score = _clip01(row["score"])
        conf = _clip01(row["confidence"])
        reason = (
            f"z20 {feat.get('z20', 0):+.2f}, RSI {feat.get('rsi', 50):.0f}, "
            f"vol30 {feat.get('vol30', 0):.0%}"
            if score > 0
            else "pas d'excès baissier exploitable"
        )
        return Signal(symbol, self.name, score, conf, reason)


class CrossSectionalAgent(Agent):
    name = "xsmom"
    role = "Surpondérer les deux actifs au momentum risque le plus fort, filtre momentum absolu positif."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.top_n = int(get(cfg, "strategy.xsmom.top_n", 2))
        self.min_dispersion = float(get(cfg, "strategy.xsmom.min_dispersion", 0.15))
        self.enabled = bool(get(cfg, "strategy.xsmom.enabled", True))
        self.frames: dict[str, pd.DataFrame] = {}
        self._store: FeatureStore | None = None
        self._dispersion: pd.Series = pd.Series(dtype=float)
        self._ranks: dict[str, pd.Series] = {}

    def compute(self, store: FeatureStore) -> dict[str, pd.DataFrame]:
        assets = store.assets
        idx = store.dates
        if not self.enabled:
            out = {
                a: pd.DataFrame({"score": 0.0, "confidence": 0.0}, index=idx)
                for a in assets
            }
            self.frames = out
            self._dispersion = pd.Series(0.0, index=idx)
            self._ranks = {a: pd.Series(np.nan, index=idx) for a in assets}
            self._store = store
            return out
        mom = pd.DataFrame({a: store.frames[a]["risk_mom"].reindex(idx) for a in assets})
        eligible = pd.DataFrame(
            {
                a: (
                    (store.frames[a]["risk_mom"] > 0)
                    & (store.frames[a]["close"] > store.frames[a]["ema_mid"])
                ).reindex(idx)
                for a in assets
            }
        )
        scores = {a: np.zeros(len(idx)) for a in assets}
        confs = {a: np.zeros(len(idx)) for a in assets}
        ranks: dict[str, pd.Series] = {a: pd.Series(np.nan, index=idx) for a in assets}
        dispersion = pd.Series(0.0, index=idx)

        for i, date in enumerate(idx):
            values = mom.iloc[i]
            valid = values[eligible.iloc[i].fillna(False)]
            valid = valid.dropna()
            if len(valid) < 2:
                continue
            dispersion.iloc[i] = float(valid.std(ddof=0))
            if dispersion.iloc[i] < self.min_dispersion:
                continue
            ordered = valid.sort_values(ascending=False)
            for rank, (symbol, value) in enumerate(ordered.items(), start=1):
                ranks[symbol].iloc[i] = rank
                if rank > self.top_n:
                    continue
                score = 1.0 if rank == 1 else 0.65
                scores[symbol][i] = score
                confs[symbol][i] = float(np.clip(dispersion.iloc[i] / 0.8, 0.0, 1.0))

        out = {
            a: pd.DataFrame(
                {"score": pd.Series(scores[a], index=idx), "confidence": pd.Series(confs[a], index=idx)}
            )
            for a in assets
        }
        self.frames = out
        self._dispersion = dispersion
        self._ranks = ranks
        self._store = store
        return out

    def signal(self, symbol: str, date: pd.Timestamp) -> Signal:
        if not self.enabled:
            return Signal(symbol, self.name, 0.0, 0.0, "sleeve désactivé")
        row = self.frames[symbol].loc[date]
        score = _clip01(row["score"])
        conf = _clip01(row["confidence"])
        rank = self._ranks[symbol].loc[date]
        disp = float(self._dispersion.loc[date])
        if score > 0 and np.isfinite(rank):
            reason = f"rang {int(rank)}/{len(self.frames)} momentum risque, dispersion {disp:.2f}"
        else:
            reason = "hors du top momentum ou dispersion insuffisante"
        return Signal(symbol, self.name, score, conf, reason)
