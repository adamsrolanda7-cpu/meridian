"""Features et régime : aucune information future ne doit fuiter."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from meridian.agents.signals import CrossSectionalAgent, MeanReversionAgent
from meridian.config import deep_merge
from meridian.features import FeatureStore, build_asset_features


def test_asset_features_causal(cfg, panel):
    btc = panel["BTCUSDT"]
    cut = len(btc) - 60
    full = build_asset_features(btc, cfg, "BTCUSDT")
    trunc = build_asset_features(btc.iloc[:cut], cfg, "BTCUSDT")
    columns = [
        "ema_fast",
        "ema_slow",
        "atr",
        "rsi",
        "adx",
        "z20",
        "don_hi",
        "breakout",
        "vol30",
        "risk_mom",
        "adv20",
        "dd90",
        "trend_stack",
    ]
    for column in columns:
        left = full[column].iloc[cut - 1]
        right = trunc[column].iloc[cut - 1]
        if pd.isna(left) and pd.isna(right):
            continue
        assert left == pytest.approx(right, rel=1e-10, nan_ok=True), column


def test_regime_causal(cfg, panel, store):
    cutoff = store.dates[store.warmup_index() + 420]
    truncated = {s: df.loc[df.index <= cutoff].copy() for s, df in panel.items()}
    small = FeatureStore.build(truncated, cfg)
    for column in ("label", "score", "confidence", "breadth", "vol_pct"):
        left = store.regime[column].loc[:cutoff]
        right = small.regime[column].loc[:cutoff]
        if column in ("score", "confidence", "breadth", "vol_pct"):
            assert np.allclose(
                left.to_numpy(dtype=float),
                right.to_numpy(dtype=float),
                equal_nan=True,
            ), column
        else:
            assert list(left) == list(right), column


def test_regime_ignores_future_prices(cfg, panel, store):
    cutoff = store.dates[store.warmup_index() + 300]
    perturbed = {}
    for symbol, df in panel.items():
        copy = df.copy()
        future = copy.index > cutoff
        for column in ("open", "high", "low", "close"):
            copy.loc[future, column] = copy.loc[future, column] * 7.0
        perturbed[symbol] = copy
    other = FeatureStore.build(perturbed, cfg)
    for column in ("label", "score", "breadth", "vol_pct"):
        left = store.regime[column].loc[:cutoff]
        right = other.regime[column].loc[:cutoff]
        if column == "label":
            assert list(left) == list(right)
        else:
            assert np.allclose(left.to_numpy(dtype=float), right.to_numpy(dtype=float), equal_nan=True)


def test_warmup_index_points_to_first_ema_slow(store):
    idx = store.warmup_index()
    assert 0 < idx < len(store.dates)
    assert pd.notna(store.regime["btc_ema_slow"].iloc[idx])
    if idx > 0:
        assert pd.isna(store.regime["btc_ema_slow"].iloc[idx - 1])


def test_store_alignment(store):
    for symbol in store.assets:
        aligned = store.aligned(symbol)
        assert len(aligned) == len(store.dates)
        assert aligned.index.equals(store.dates)


def test_bear_breadth_override_is_configurable(cfg, panel):
    base = FeatureStore.build(
        panel, deep_merge(cfg, {"strategy": {"regime": {"bear_breadth": 0.0}}})
    )
    bear = FeatureStore.build(
        panel, deep_merge(cfg, {"strategy": {"regime": {"bear_breadth": 1.0}}})
    )
    changed = base.regime["raw_label"] != bear.regime["raw_label"]
    assert changed.any()
    assert (bear.regime.loc[changed, "raw_label"] == "TREND_DOWN").all()


def test_meanrev_can_be_suspended(cfg, store):
    agent = MeanReversionAgent(
        deep_merge(cfg, {"strategy": {"meanrev": {"enabled": False}}})
    )
    frames = agent.compute(store)
    assert frames
    assert all(float(frame["score"].abs().max()) == 0.0 for frame in frames.values())


def test_xsmom_can_be_suspended(cfg, store):
    agent = CrossSectionalAgent(
        deep_merge(cfg, {"strategy": {"xsmom": {"enabled": False}}})
    )
    frames = agent.compute(store)
    assert frames
    assert all(float(frame["score"].abs().max()) == 0.0 for frame in frames.values())
