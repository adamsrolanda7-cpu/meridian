"""Métriques et configuration : pas d'arrondi complaisant."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from meridian.config import deep_merge, dump_config, get, load_config, set_in, validate_config
from meridian.metrics import format_metrics, monthly_returns, performance, yearly_returns


def test_metrics_on_known_series():
    dates = pd.date_range("2025-01-01", periods=366, freq="D", tz="UTC")
    equity = pd.Series(100_000 * (1.0005 ** np.arange(366)), index=dates)
    metrics = performance(equity)
    assert metrics["final_equity"] == pytest.approx(equity.iloc[-1])
    assert metrics["total_return"] == pytest.approx(equity.iloc[-1] / equity.iloc[0] - 1)
    assert metrics["volatility"] >= 0
    assert metrics["max_drawdown"] <= 0
    assert metrics["days"] == 365


def test_metrics_drawdown_and_benchmark():
    dates = pd.date_range("2025-01-01", periods=200, freq="D", tz="UTC")
    equity = pd.Series(np.linspace(100_000, 120_000, 200), index=dates)
    equity.iloc[120:] = np.linspace(110_000, 90_000, 80)
    benchmark = pd.Series(np.linspace(60_000, 50_000, 200), index=dates)
    metrics = performance(equity, benchmark=benchmark)
    assert metrics["max_drawdown"] < -0.10
    assert "benchmark" in metrics
    assert np.isfinite(metrics["benchmark"]["beta"])


def test_monthly_and_yearly_returns():
    dates = pd.date_range("2024-01-01", "2025-12-31", freq="D", tz="UTC")
    equity = pd.Series(np.linspace(100.0, 200.0, len(dates)), index=dates)
    monthly = monthly_returns(equity)
    assert len(monthly) >= 23
    assert monthly["return"].notna().all()
    yearly = yearly_returns(equity)
    assert len(yearly) == 2
    assert yearly.iloc[0] > 0


def test_format_metrics_contains_key_labels():
    dates = pd.date_range("2025-01-01", periods=100, freq="D", tz="UTC")
    equity = pd.Series(np.linspace(100_000, 101_000, 100), index=dates)
    text = format_metrics(performance(equity))
    for label in ("CAGR", "Sharpe", "Max drawdown", "Volatilité"):
        assert label in text


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    merged = deep_merge(base, {"a": {"b": 9}, "e": 4})
    assert base["a"]["b"] == 1
    assert merged["a"]["b"] == 9
    assert merged["a"]["c"] == 2
    assert merged["e"] == 4


def test_get_and_set_dotted():
    cfg = {"a": {"b": {"c": 1}}}
    assert get(cfg, "a.b.c") == 1
    assert get(cfg, "a.x", "fallback") == "fallback"
    set_in(cfg, "a.b.c", 5)
    assert cfg["a"]["b"]["c"] == 5


def test_validate_rejects_short_and_bad_leverage():
    cfg = load_config()
    cfg["strategy"]["allow_short"] = True
    assert any("allow_short" in e for e in validate_config(cfg))
    cfg = load_config()
    cfg["strategy"]["min_cash"] = 0.20
    cfg["strategy"]["max_gross"] = 0.90
    assert any("min_cash" in e for e in validate_config(cfg))


def test_dump_and_load_config(tmp_path, cfg):
    path = tmp_path / "cfg.json"
    dump_config(cfg, str(path))
    reloaded = load_config(str(path))
    assert reloaded["universe"] == cfg["universe"]
    assert reloaded["strategy"]["target_vol"] == cfg["strategy"]["target_vol"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["name"] == cfg["name"]
