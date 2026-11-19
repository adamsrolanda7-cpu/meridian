"""Construction des features et de l'état de marché.

Chaque colonne est calculée uniquement à partir du passé de la bougie. Le
FeatureStore aligne les actifs sur un calendrier commun et prépare les séries
de régime utilisées par les agents.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ta
from .config import get


def build_asset_features(df: pd.DataFrame, cfg: dict, symbol: str) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out["symbol"] = symbol
    out["open"] = df["open"]
    out["high"] = df["high"]
    out["low"] = df["low"]
    out["close"] = df["close"]
    out["volume"] = df["volume"]
    out["quote_volume"] = df.get("quote_volume", df["volume"] * df["close"])

    out["ret1"] = df["close"].pct_change()
    out["ema_fast"] = ta.ema(df["close"], int(get(cfg, "strategy.trend.ema_fast", 50)))
    out["ema_slow"] = ta.ema(df["close"], int(get(cfg, "strategy.trend.ema_slow", 200)))
    out["ema_mid"] = ta.ema(df["close"], 100)
    out["atr"] = ta.atr(df, int(get(cfg, "risk.atr_win", 14)))
    out["atr_pct"] = out["atr"] / df["close"]
    out["rsi"] = ta.rsi(df["close"], 14)
    adx_w, plus_di, minus_di = ta.adx(df, 14)
    out["adx"] = adx_w
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    out["bb_mid"], _, _, out["bb_std"] = ta.bollinger(
        df["close"], int(get(cfg, "strategy.meanrev.bb_win", 20))
    )
    out["z20"] = ta.zscore(df["close"], int(get(cfg, "strategy.meanrev.bb_win", 20)))

    breakout_win = int(get(cfg, "strategy.trend.breakout_win", 55))
    out["don_hi"], out["don_lo"] = ta.donchian(df, breakout_win)
    out["breakout"] = (df["close"] > out["don_hi"]).astype(float)
    out.loc[out["don_hi"].isna(), "breakout"] = np.nan

    out["vol30"] = ta.realized_vol(out["ret1"], 30)
    out["vol90"] = ta.realized_vol(out["ret1"], 90)
    mom_win = int(get(cfg, "strategy.trend.mom_win", 90))
    out["mom30"] = df["close"].pct_change(30)
    out["mom90"] = df["close"].pct_change(mom_win)
    denom = (out["vol30"] * np.sqrt(mom_win / 365.0)).replace(0.0, np.nan)
    out["risk_mom"] = out["mom90"] / denom
    out["mom20"] = df["close"].pct_change(20)

    out["adv20"] = out["quote_volume"].rolling(20, min_periods=10).mean()
    out["dd90"] = df["close"] / df["close"].rolling(90, min_periods=30).max() - 1.0
    out["above_slow"] = (df["close"] > out["ema_slow"]).astype(float)
    out["above_mid"] = (df["close"] > out["ema_mid"]).astype(float)
    stack = np.where(
        (df["close"] > out["ema_fast"]) & (out["ema_fast"] > out["ema_slow"]),
        1.0,
        np.where((df["close"] < out["ema_fast"]) & (out["ema_fast"] < out["ema_slow"]), -1.0, 0.0),
    )
    out["trend_stack"] = pd.Series(stack, index=df.index).where(out["ema_slow"].notna())
    out["above_slow"] = out["above_slow"].where(out["ema_slow"].notna())
    out["above_mid"] = out["above_mid"].where(out["ema_mid"].notna())
    return out


def build_regime_frame(store: "FeatureStore", cfg: dict) -> pd.DataFrame:
    """Régime global : BTC comme ancre + largeur du marché.

    CRISIS      : vol extrême + drawdown ou choc de momentum
    TREND_UP    : BTC au-dessus de ses moyennes, ADX suffisant, breadth > 50%
    TREND_DOWN  : configuration inverse
    RANGE       : tout le reste
    """
    btc = store.frames["BTCUSDT"]
    idx = store.dates
    out = pd.DataFrame(index=idx)
    for col in ("close", "ema_fast", "ema_slow", "adx", "vol30", "dd90", "mom20"):
        out[f"btc_{col}"] = btc[col].reindex(idx)

    vol_pct = ta.rolling_percentile(out["btc_vol30"], 365, min_periods=90)
    out["vol_pct"] = vol_pct

    flags = []
    for symbol, frame in store.frames.items():
        flags.append((frame["close"] > frame["ema_mid"]).reindex(idx).astype(float))
    breadth_df = pd.concat(flags, axis=1)
    available = breadth_df.notna().sum(axis=1)
    out["breadth"] = breadth_df.mean(axis=1, skipna=True)
    out.loc[available == 0, "breadth"] = np.nan

    adx_min = float(get(cfg, "strategy.regime.adx_trend", 15.0))
    breadth_thr = float(get(cfg, "strategy.regime.breadth", 0.5))
    vol_thr = float(get(cfg, "strategy.regime.vol_crisis_pct", 0.90))
    dd_thr = float(get(cfg, "strategy.regime.dd_crisis", 0.15))
    mom_thr = float(get(cfg, "strategy.regime.mom_crisis", 0.10))

    up = (
        (out["btc_close"] > out["btc_ema_slow"])
        & (out["btc_ema_fast"] > out["btc_ema_slow"])
        & (out["btc_adx"] >= adx_min)
        & (out["breadth"] >= breadth_thr)
    )
    down = (
        (out["btc_close"] < out["btc_ema_slow"])
        & (out["btc_ema_fast"] < out["btc_ema_slow"])
        & (out["btc_adx"] >= adx_min)
        & (out["breadth"] <= 1.0 - breadth_thr)
    )
    # Override baissier précoce : si BTC est sous son EMA200 et que la
    # majorité du marché est sous son EMA100, on ne veut pas attendre le
    # croisement EMA50/EMA200 pour passer en risk-off. Sans cet override,
    # un marché qui casse peut rester classé RANGE pendant des semaines et
    # le sleeve mean-reversion achète des couteaux qui tombent.
    bear_breadth = float(get(cfg, "strategy.regime.bear_breadth", 0.25))
    bear = (
        (bear_breadth > 0.0)
        & (out["btc_close"] < out["btc_ema_slow"])
        & (out["breadth"] <= bear_breadth)
        & out["btc_ema_slow"].notna()
        & out["breadth"].notna()
    )
    down = down | bear

    crisis = (
        (vol_pct >= vol_thr)
        & ((out["btc_dd90"] <= -dd_thr) | (out["btc_mom20"] <= -mom_thr))
    )

    raw = pd.Series("RANGE", index=idx, dtype=object)
    raw[up] = "TREND_UP"
    raw[down] = "TREND_DOWN"
    raw[crisis] = "CRISIS"
    # Pas de régime tant que l'historique BTC n'est pas suffisant.
    raw[out["btc_ema_slow"].isna()] = "WARMUP"

    confirm = int(get(cfg, "strategy.regime.confirm_bars", 2))
    labels: list[str] = []
    current = "WARMUP"
    pending = 0
    for label in raw:
        if label == current:
            pending = 0
        else:
            pending += 1
            if pending >= max(1, confirm):
                current = label
                pending = 0
        labels.append(current)
    out["raw_label"] = raw
    out["label"] = labels

    score_map = {"TREND_UP": 1.0, "RANGE": 0.0, "TREND_DOWN": -1.0, "CRISIS": -1.0, "WARMUP": 0.0}
    out["score"] = out["label"].map(score_map)

    conf = []
    for _, row in out.iterrows():
        label = row["label"]
        adx = row["btc_adx"] if pd.notna(row["btc_adx"]) else 20.0
        breadth = row["breadth"] if pd.notna(row["breadth"]) else 0.5
        vp = row["vol_pct"] if pd.notna(row["vol_pct"]) else 0.5
        if label == "TREND_UP":
            value = 0.45 + 0.30 * min(1.0, adx / 40.0) + 0.25 * min(1.0, max(0.0, (breadth - 0.5) / 0.5))
        elif label == "TREND_DOWN":
            value = 0.45 + 0.30 * min(1.0, adx / 40.0) + 0.25 * min(1.0, max(0.0, (0.5 - breadth) / 0.5))
        elif label == "CRISIS":
            value = 0.65 + 0.35 * min(1.0, max(0.0, vp))
        elif label == "WARMUP":
            value = 0.0
        else:
            value = 0.45 + 0.30 * (1.0 - min(1.0, adx / 40.0)) + 0.25 * (1.0 - min(1.0, abs(breadth - 0.5) * 2))
        conf.append(float(np.clip(value, 0.0, 1.0)))
    out["confidence"] = conf

    reasons = []
    for _, row in out.iterrows():
        label = row["label"]
        if label == "TREND_UP":
            text = (
                f"BTC > EMA200, EMA50 > EMA200, ADX {row['btc_adx']:.0f}, "
                f"breadth {row['breadth']:.0%}"
            )
        elif label == "TREND_DOWN":
            text = (
                f"BTC < EMA200, EMA50 < EMA200, ADX {row['btc_adx']:.0f}, "
                f"breadth {row['breadth']:.0%} → cash"
            )
        elif label == "CRISIS":
            text = (
                f"vol {row['vol_pct']:.0%} percentile, DD90 {row['btc_dd90']:.0%} → cash"
            )
        elif label == "WARMUP":
            text = "historique insuffisant"
        else:
            text = (
                f"pas de tendance directionnelle nette (ADX {row['btc_adx']:.0f}, "
                f"breadth {row['breadth']:.0%})"
            )
        reasons.append(text)
    out["reason"] = reasons
    return out


@dataclass
class FeatureStore:
    cfg: dict
    panel: dict[str, pd.DataFrame]
    assets: list[str]
    frames: dict[str, pd.DataFrame]
    dates: pd.DatetimeIndex
    regime: pd.DataFrame

    @classmethod
    def build(cls, panel: dict[str, pd.DataFrame], cfg: dict) -> "FeatureStore":
        assets = [s for s in cfg["universe"] if s in panel and len(panel[s]) > 30]
        if "BTCUSDT" not in assets:
            raise ValueError("BTCUSDT est requis comme actif de référence pour le régime")
        frames = {s: build_asset_features(panel[s], cfg, s) for s in assets}
        dates = pd.DatetimeIndex(sorted(set().union(*[set(f.index) for f in frames.values()])))
        store = cls(cfg=cfg, panel=panel, assets=assets, frames=frames, dates=dates, regime=pd.DataFrame())
        store.regime = build_regime_frame(store, cfg)
        return store

    def aligned(self, symbol: str) -> pd.DataFrame:
        return self.frames[symbol].reindex(self.dates)

    def warmup_index(self) -> int:
        """Premier indice où les features de tendance sont disponibles."""
        valid = self.regime["btc_ema_slow"].notna().to_numpy()
        if not valid.any():
            return len(self.dates)
        return int(np.argmax(valid))

    def slice(self, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> "FeatureStore":
        """Sous-ensemble temporel, features conservées (warmup préservé)."""
        mask = np.ones(len(self.dates), dtype=bool)
        if start is not None:
            mask &= self.dates >= pd.Timestamp(start, tz="UTC")
        if end is not None:
            mask &= self.dates <= pd.Timestamp(end, tz="UTC")
        dates = self.dates[mask]
        return FeatureStore(
            cfg=self.cfg,
            panel=self.panel,
            assets=self.assets,
            frames={s: self.frames[s].reindex(dates) for s in self.assets},
            dates=dates,
            regime=self.regime.reindex(dates),
        )
