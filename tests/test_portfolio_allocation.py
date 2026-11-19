"""Allocation : plafonds, bandes, inverse-vol, ciblage de volatilité."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from meridian.agents.portfolio import PortfolioAgent
from meridian.config import deep_merge
from meridian.types import Regime


def make_regime(label: str = "RANGE") -> Regime:
    return Regime(label=label, score=0.0, confidence=0.7, breadth=0.5, vol_pct=0.4, adx=20.0, reason="test")


def make_scores(assets, value: float = 0.8, conf: float = 0.9, sleeve: str = "meanrev"):
    out = {}
    for asset in assets:
        out[asset] = {"trend": (0.0, 0.0), "meanrev": (0.0, 0.0), "xsmom": (0.0, 0.0)}
        out[asset][sleeve] = (value, conf)
    return out


def make_vols(assets, vols=None):
    vols = vols or {a: 0.5 for a in assets}
    return vols


def make_cov(assets, vols):
    return np.diag([vols[a] ** 2 / 365.0 for a in assets])


def build(agent, scores, vols, assets, current=None, regime="RANGE", equity=100_000.0):
    vols = {asset: float(vols.get(asset, 0.5)) for asset in assets}
    cov = make_cov(assets, vols)
    return agent.build(
        date=pd.Timestamp("2026-01-01", tz="UTC"),
        regime=make_regime(regime),
        scores=scores,
        vols=vols,
        cov=cov,
        assets=assets,
        current=current or {},
        equity=equity,
        adv={a: 1e12 for a in assets},
    )


def test_targets_respect_mandate(cfg):
    assets = cfg["universe"]
    agent = PortfolioAgent(cfg)
    vols = make_vols(assets, {"BTCUSDT": 0.40, "ETHUSDT": 0.55, "SOLUSDT": 0.80, "LTCUSDT": 0.60})
    allocation = build(agent, make_scores(assets), vols, assets)
    weights = {t.asset: t.weight for t in allocation.targets}
    assert all(w >= 0 for w in weights.values())
    assert sum(weights.values()) <= cfg["strategy"]["max_gross"] + 1e-9
    assert all(w <= cfg["strategy"]["max_asset_weight"] + 1e-9 for w in weights.values())
    assert allocation.portfolio_vol <= cfg["strategy"]["target_vol"] + 1e-6
    assert allocation.gross == pytest.approx(sum(weights.values()))


def test_inverse_vol_tilts_to_low_vol(cfg):
    assets = cfg["universe"]
    agent = PortfolioAgent(cfg)
    vols = {"BTCUSDT": 0.20, "ETHUSDT": 0.40, "SOLUSDT": 0.80, "LTCUSDT": 0.80}
    allocation = build(agent, make_scores(assets), vols, assets)
    weights = {t.asset: t.weight for t in allocation.targets}
    assert weights["BTCUSDT"] > weights["SOLUSDT"]
    assert weights["ETHUSDT"] > weights["LTCUSDT"]


def test_vol_targeting_scales_down(cfg):
    assets = cfg["universe"]
    agent = PortfolioAgent(cfg)
    vols = {a: 1.5 for a in assets}
    allocation = build(agent, make_scores(assets), vols, assets)
    assert allocation.portfolio_vol <= cfg["strategy"]["target_vol"] + 1e-6
    assert sum(t.weight for t in allocation.targets) < cfg["strategy"]["max_gross"]


def test_entry_band_blocks_weak_signal(cfg):
    assets = cfg["universe"]
    agent = PortfolioAgent(cfg)
    scores = make_scores(assets, value=0.80)
    for asset in assets:
        scores[asset]["meanrev"] = (0.10, 0.9)  # sous exit_score et entry_score
    allocation = build(agent, scores, make_vols(assets), assets)
    assert all(t.weight == 0.0 for t in allocation.targets)


def test_exit_band_forces_held_asset_out(cfg):
    assets = cfg["universe"]
    agent = PortfolioAgent(cfg)
    scores = make_scores(assets, value=0.80)
    scores["BTCUSDT"]["meanrev"] = (0.10, 0.9)  # > exit_score? non, sous 0.12
    allocation = build(agent, scores, make_vols(assets), assets, current={"BTCUSDT": 0.20})
    weights = {t.asset: t.weight for t in allocation.targets}
    assert weights["BTCUSDT"] == 0.0
    assert weights["ETHUSDT"] > 0


def test_held_asset_between_bands_is_kept(cfg):
    assets = cfg["universe"]
    agent = PortfolioAgent(cfg)
    scores = make_scores(assets, value=0.80)
    scores["BTCUSDT"]["meanrev"] = (0.20, 0.9)  # > exit 0.12, < entry 0.25
    allocation = build(agent, scores, make_vols(assets), assets, current={"BTCUSDT": 0.15})
    weights = {t.asset: t.weight for t in allocation.targets}
    assert weights["BTCUSDT"] > 0


def test_risk_off_regimes_go_to_cash(cfg):
    assets = cfg["universe"]
    agent = PortfolioAgent(cfg)
    for label in ("TREND_DOWN", "CRISIS", "WARMUP"):
        allocation = build(agent, make_scores(assets), make_vols(assets), assets, regime=label)
        assert all(t.weight == 0.0 for t in allocation.targets)
        assert allocation.gross == 0.0


def test_asset_caps_are_enforced(cfg):
    assets = cfg["universe"]
    custom = deep_merge(cfg, {"strategy": {"asset_caps": {"BTCUSDT": 0.10}}})
    agent = PortfolioAgent(custom)
    allocation = build(agent, make_scores(assets), make_vols(assets), assets)
    weights = {t.asset: t.weight for t in allocation.targets}
    assert weights["BTCUSDT"] <= 0.10 + 1e-9
    assert sum(weights.values()) <= custom["strategy"]["max_gross"] + 1e-9


def test_gross_cap_is_enforced(cfg):
    assets = cfg["universe"]
    custom = deep_merge(cfg, {"strategy": {"max_gross": 0.50, "min_cash": 0.50}})
    agent = PortfolioAgent(custom)
    allocation = build(agent, make_scores(assets), make_vols(assets), assets)
    assert sum(t.weight for t in allocation.targets) <= 0.50 + 1e-9
