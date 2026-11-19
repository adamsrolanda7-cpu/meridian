"""Recherche : split honnête, Monte Carlo reproductible, score pénalisant."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from meridian.research import _fold_ranges, _jsonable, _score, monte_carlo, split_dates, window_metrics


def test_split_starts_at_warmup(store):
    start, cut = split_dates(store, 0.70)
    assert start == str(store.dates[store.warmup_index()].date())
    assert cut > start
    assert cut < str(store.dates[-1].date())


def test_fold_ranges_are_contiguous_and_semiannual():
    start = pd.Timestamp("2020-01-01", tz="UTC")
    end = pd.Timestamp("2026-01-01", tz="UTC")
    folds = _fold_ranges(start, end, folds=8, test_days=182)
    assert len(folds) == 8
    for (_, previous_end), (next_start, _) in zip(folds, folds[1:]):
        assert pd.Timestamp(next_start) == pd.Timestamp(previous_end) + pd.Timedelta(days=1)
    assert folds[-1][1] == str(end.date())
    assert all(
        (pd.Timestamp(b) - pd.Timestamp(a)).days == 181 for a, b in folds
    )


def test_window_metrics_distinguishes_fresh_and_continuous():
    index = pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC")
    returns = np.concatenate(
        [
            np.full(100, 0.002),
            np.full(200, -0.001),
        ]
    )
    returns[200] = -0.10
    equity = pd.Series(100_000 * (1 + returns).cumprod(), index=index)
    start, end = index[150], index[-1]
    fresh = window_metrics(equity, start, end, fresh=True)
    continuous = window_metrics(equity, start, end, fresh=False)
    assert fresh["cagr"] == pytest.approx(continuous["cagr"])
    # Le drawdown continu compte le plus haut atteint avant la fenêtre.
    assert continuous["max_drawdown"] < fresh["max_drawdown"]
    assert continuous["dd_at_end"] == pytest.approx(continuous["max_drawdown"])


def test_monte_carlo_is_deterministic_and_ordered():
    rng = np.random.default_rng(3)
    returns = pd.Series(rng.normal(0.0004, 0.01, 700))
    equity = 100_000 * (1 + returns).cumprod()
    first = monte_carlo(equity, paths=200, block=20, seed=5)
    second = monte_carlo(equity, paths=200, block=20, seed=5)
    assert first["cagr_p5"] == second["cagr_p5"]
    assert first["cagr_p5"] <= first["cagr_p50"] <= first["cagr_p95"]
    assert first["maxdd_p5"] <= first["maxdd_p50"] <= first["maxdd_p95"]
    assert 0.0 <= first["prob_negative"] <= 1.0
    assert first["paths"] == 200


def test_score_rejects_bad_tail_and_turnover():
    good = {"sharpe": 1.0, "max_drawdown": -0.10, "turnover_x": 4.0, "round_trips": 100}
    bad_dd = {"sharpe": 1.5, "max_drawdown": -0.50, "turnover_x": 4.0, "round_trips": 100}
    bad_turnover = {"sharpe": 1.5, "max_drawdown": -0.10, "turnover_x": 20.0, "round_trips": 100}
    too_few = {"sharpe": 1.5, "max_drawdown": -0.10, "turnover_x": 4.0, "round_trips": 3}
    assert _score(good) > 0
    assert _score(bad_dd) < 0
    assert _score(bad_turnover) < 0
    assert _score(too_few) < 0


def test_jsonable_handles_numpy_and_nan():
    payload = {"a": np.float64(1.5), "b": np.int64(3), "c": float("nan"), "d": [np.float64(2.0)]}
    out = _jsonable(payload)
    assert out["a"] == 1.5
    assert out["b"] == 3
    assert out["c"] is None
    assert out["d"] == [2.0]
