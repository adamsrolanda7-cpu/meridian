"""Backtest : invariants, déterminisme et absence de look-ahead."""
from __future__ import annotations

import numpy as np
import pandas as pd

from meridian.backtest import Backtest
from meridian.config import deep_merge
from meridian.features import FeatureStore


def _compare_equity(left: pd.Series, right: pd.Series, cutoff) -> None:
    common = left.loc[:cutoff].index.intersection(right.loc[:cutoff].index)
    assert len(common) > 100
    assert np.allclose(left.loc[common].to_numpy(), right.loc[common].to_numpy(), rtol=1e-9, atol=1e-6)


def test_run_is_deterministic(engine):
    first = engine.run()
    second = engine.run()
    assert first.equity.iloc[-1] == second.equity.iloc[-1]
    assert len(first.trades) == len(second.trades)
    assert first.metrics["sharpe"] == second.metrics["sharpe"]


def test_book_invariants(engine, cfg):
    result = engine.run()
    assert (result.equity > 0).all()
    assert result.book.cash >= -1e-6
    assert result.book.fees_paid >= 0
    assert result.gross.max() < 3.0  # pas de levier extrême par dérive
    for pos in result.book.positions.values():
        assert pos.qty >= 0
        assert pos.stop > 0
    for decision in result.decisions:
        assert decision["gross_target"] <= cfg["strategy"]["max_gross"] + 1e-6
        for weight in decision["targets"].values():
            assert weight <= cfg["strategy"]["max_asset_weight"] + 1e-6


def test_no_lookahead_truncated_history(engine, cfg, panel, store):
    cutoff = store.dates[store.warmup_index() + 500]
    truncated = {s: df.loc[df.index <= cutoff].copy() for s, df in panel.items()}
    small = FeatureStore.build(truncated, cfg)
    result_trunc = Backtest(small, cfg).run(end=cutoff)
    result_full = engine.run()
    _compare_equity(result_full.equity, result_trunc.equity, cutoff)


def test_no_lookahead_future_prices_perturbed(engine, cfg, panel, store):
    cutoff = store.dates[store.warmup_index() + 450]
    perturbed = {}
    for symbol, df in panel.items():
        copy = df.copy()
        future = copy.index > cutoff
        for column in ("open", "high", "low", "close"):
            copy.loc[future, column] = copy.loc[future, column] * 3.0
        perturbed[symbol] = copy
    other = FeatureStore.build(perturbed, cfg)
    result_other = Backtest(other, cfg).run()
    result_full = engine.run()
    _compare_equity(result_full.equity, result_other.equity, cutoff)


def test_start_index_respects_warmup_and_regime(engine, store):
    idx = engine._start_index(None)
    assert idx >= store.warmup_index()
    assert store.regime["label"].iloc[idx] != "WARMUP"


def test_cost_multiplier_reduces_equity(engine):
    base = engine.run(cost_multiplier=1.0)
    stressed = engine.run(cost_multiplier=4.0)
    assert stressed.equity.iloc[-1] <= base.equity.iloc[-1]
    assert stressed.book.fees_paid >= base.book.fees_paid


def test_gross_cap_config_is_honoured(engine, cfg):
    custom = deep_merge(cfg, {"strategy": {"max_gross": 0.40, "min_cash": 0.60}})
    result = Backtest(engine.store, custom).run()
    for decision in result.decisions:
        assert decision["gross_target"] <= 0.40 + 1e-6
