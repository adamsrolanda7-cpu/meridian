"""Risque : gouverneur drawdown, stops, veto et plafonds."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from meridian.agents.risk import RiskAgent
from meridian.config import deep_merge
from meridian.types import Position, Target


@pytest.fixture()
def risk(cfg):
    return RiskAgent(cfg)


def test_governor_derisk_halt_probation_and_rearm(cfg):
    agent = RiskAgent(cfg)
    assert agent.update(100_000, 0).scale == 1.0
    assert agent.update(95_000, 1).scale == 1.0
    assert agent.update(91_000, 2).scale == 0.5  # -9% ≤ -8%
    halted = agent.update(87_000, 3)  # -13% ≤ -12%
    assert halted.halted and halted.scale == 0.0
    # Fin du gel : probation à risque réduit, pas de blocage à vie.
    probation = agent.update(87_000, 3 + cfg["risk"]["halt_bars"])
    assert not probation.halted
    assert probation.scale == pytest.approx(cfg["risk"]["probation_scale"])
    # Le drawdown ne s'aggrave pas de 5 points : pas de nouveau gel.
    again = agent.update(87_000, 4 + cfg["risk"]["halt_bars"])
    assert not again.halted
    # Aggravation de 5 points : nouveau gel.
    rehalt = agent.update(79_000, 5 + cfg["risk"]["halt_bars"])
    assert rehalt.halted


def test_governor_rearms_after_recovery(cfg):
    agent = RiskAgent(cfg)
    agent.update(100_000, 0)
    agent.update(86_000, 1)
    assert agent.halted
    agent.update(86_000, 1 + cfg["risk"]["halt_bars"])
    assert not agent.halted
    agent.update(95_000, 2 + cfg["risk"]["halt_bars"])
    assert agent.last_halt_dd == 0.0
    agent.update(87_000, 3 + cfg["risk"]["halt_bars"])
    assert agent.halted


def test_daily_loss_blocks_new_risk(cfg):
    agent = RiskAgent(cfg)
    agent.update(100_000, 0)
    governor = agent.update(96_000, 1)  # -4% ≤ -3%
    assert governor.no_new_risk
    targets = [Target("BTCUSDT", 0.30), Target("ETHUSDT", 0.30)]
    out, notes = agent.apply(targets, governor, {"BTCUSDT": 0.10}, 1)
    weights = {t.asset: t.weight for t in out}
    assert weights["BTCUSDT"] == pytest.approx(0.10)
    assert weights["ETHUSDT"] == 0.0
    assert any("perte journalière" in n for n in notes)


def test_apply_caps_and_gross(cfg):
    custom = deep_merge(cfg, {"strategy": {"max_gross": 0.50, "max_asset_weight": 0.40}})
    agent = RiskAgent(custom)
    governor = agent.update(100_000, 0)
    targets = [Target("BTCUSDT", 0.90), Target("ETHUSDT", 0.90)]
    out, notes = agent.apply(targets, governor, {}, 0)
    weights = {t.asset: t.weight for t in out}
    assert all(w <= 0.40 + 1e-9 for w in weights.values())
    assert sum(weights.values()) <= 0.50 + 1e-9
    assert any("gross plafonné" in n for n in notes)


def test_cooldown_blocks_asset(cfg):
    agent = RiskAgent(cfg)
    governor = agent.update(100_000, 0)
    agent.set_cooldown("BTCUSDT", 0)
    out, notes = agent.apply([Target("BTCUSDT", 0.30), Target("ETHUSDT", 0.20)], governor, {}, 3)
    weights = {t.asset: t.weight for t in out}
    assert weights["BTCUSDT"] == 0.0
    assert weights["ETHUSDT"] > 0
    assert any("cooldown" in n for n in notes)
    # Après la fenêtre, l'actif redevient éligible.
    assert "BTCUSDT" not in agent.blocked(0 + cfg["risk"]["cooldown_bars"])


def test_halt_liquidates_everything(cfg):
    agent = RiskAgent(cfg)
    agent.update(100_000, 0)
    governor = agent.update(80_000, 1)
    assert governor.halted
    out, notes = agent.apply([Target("BTCUSDT", 0.30)], governor, {"BTCUSDT": 0.30}, 1)
    assert out[0].weight == 0.0
    assert any("liquidé" in n for n in notes)


def test_stops_depend_on_sleeve(cfg):
    agent = RiskAgent(cfg)
    price, atr = 100.0, 2.0
    assert agent.entry_stop(price, atr, "meanrev") == pytest.approx(100 - 2 * atr)
    assert agent.entry_stop(price, atr, "trend") == pytest.approx(100 - 6 * atr)
    assert agent.entry_stop(price, atr, "xsmom") == pytest.approx(100 - 6 * atr)
    # ATR indisponible : fallback prudent à 3 % du prix.
    assert agent.entry_stop(price, np.nan, "trend") == pytest.approx(100 - 6 * 3.0)


def test_trailing_stop_only_for_trend(cfg):
    agent = RiskAgent(cfg)
    trend = Position("BTCUSDT", 1.0, 100.0, 95.0, sleeve="trend", last_price=100.0, high_water=100.0)
    agent.update_trailing(trend, close=120.0, atr=2.0)
    assert trend.stop == pytest.approx(max(95.0, 120.0 - 6 * 2.0))
    before = trend.stop
    agent.update_trailing(trend, close=110.0, atr=2.0)
    assert trend.stop == pytest.approx(before)  # un trailing ne redescend jamais
    mr = Position("ETHUSDT", 1.0, 100.0, 95.0, sleeve="meanrev", last_price=100.0, high_water=100.0)
    agent.update_trailing(mr, close=200.0, atr=2.0)
    assert mr.stop == pytest.approx(95.0)


def test_pretrade_checks(cfg):
    agent = RiskAgent(cfg)
    prices = {"BTCUSDT": 60_000.0, "ETHUSDT": 3_000.0}
    ok, reason = agent.pretrade_check("BTCUSDT", 0.30, 100_000, 1e12, prices)
    assert ok, reason
    ok, reason = agent.pretrade_check("BTCUSDT", 0.90, 100_000, 1e12, prices)
    assert not ok and "plafond" in reason
    ok, reason = agent.pretrade_check("BTCUSDT", 0.30, 100_000, 100_000, prices)
    assert not ok and "liquidité" in reason
    ok, reason = agent.pretrade_check("BTCUSDT", 0.30, 100_000, 1e12, {})
    assert not ok and "prix" in reason
