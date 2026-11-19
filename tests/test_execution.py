"""Exécution : ventes d'abord, cash plancher, coûts, liquidité."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from meridian.agents.execution import ExecutionAgent
from meridian.portfolio import Book
from meridian.types import OrderIntent


TS = pd.Timestamp("2026-01-01", tz="UTC")


def test_sells_before_buys_and_cash_floor(cfg):
    agent = ExecutionAgent(cfg)
    book = Book(100_000.0)
    book.buy(TS, "BTCUSDT", 5.0, 10_000.0, fee=5.0, stop=8_000.0, sleeve="trend")
    prices = {"BTCUSDT": 10_000.0, "ETHUSDT": 1_000.0}
    intents = [OrderIntent("BTCUSDT", 0.0), OrderIntent("ETHUSDT", 0.30)]
    report = agent.execute(
        intents,
        book,
        prices,
        adv={"BTCUSDT": 1e12, "ETHUSDT": 1e12},
        ts=TS,
        bar_index=10,
        stops={"ETHUSDT": 900.0},
        sleeves={"ETHUSDT": "trend"},
    )
    assert "BTCUSDT" not in book.positions
    assert "ETHUSDT" in book.positions
    equity = book.equity(prices)
    assert book.weights(prices)["ETHUSDT"] == pytest.approx(0.30, abs=0.01)
    assert book.cash >= cfg["strategy"]["min_cash"] * equity - 1.0
    assert not any(f.side == "BUY" for f in report.fills[:0])
    assert report.fills[0].side == "SELL"


def test_min_notional_skips_tiny_order(cfg):
    agent = ExecutionAgent(cfg)
    book = Book(100_000.0)
    intents = [OrderIntent("BTCUSDT", 0.0001)]
    before = book.cash
    report = agent.execute(
        intents,
        book,
        {"BTCUSDT": 60_000.0},
        adv={"BTCUSDT": 1e12},
        ts=TS,
        bar_index=1,
    )
    assert not report.fills
    assert book.cash == pytest.approx(before)
    assert any("notionnel minimum" in n for n in report.notes)


def test_impact_increases_and_is_capped(cfg):
    agent = ExecutionAgent(cfg)
    small = agent._impact(1_000.0, 1e9)
    large = agent._impact(1e9, 1e9)
    assert 0 <= small < large
    assert agent._impact(1e12, 1e6) <= 150.0


def test_liquidity_cap_limits_fill(cfg):
    agent = ExecutionAgent(cfg)
    book = Book(100_000.0)
    intents = [OrderIntent("BTCUSDT", 0.50)]
    report = agent.execute(
        intents,
        book,
        {"BTCUSDT": 50_000.0},
        adv={"BTCUSDT": 200_000.0},  # 5% = 10k max
        ts=TS,
        bar_index=1,
    )
    assert report.fills
    assert report.fills[0].notional <= 200_000.0 * cfg["risk"]["max_adv_pct"] + 1e-6


def test_liquidate_clears_all_positions(cfg):
    agent = ExecutionAgent(cfg)
    book = Book(100_000.0)
    book.buy(TS, "BTCUSDT", 1.0, 10_000.0, fee=1.0, stop=8_000.0, sleeve="trend")
    book.buy(TS, "ETHUSDT", 10.0, 1_000.0, fee=1.0, stop=800.0, sleeve="trend")
    report = agent.liquidate(
        book,
        {"BTCUSDT": 10_000.0, "ETHUSDT": 1_000.0},
        {"BTCUSDT": 1e12, "ETHUSDT": 1e12},
        TS,
        reason="test liquidation",
    )
    assert not book.positions
    assert len(report.fills) == 2
    assert all(f.side == "SELL" for f in report.fills)
    assert book.cash > 0


def test_slippage_and_fees_are_charged(cfg):
    agent = ExecutionAgent(cfg)
    book = Book(100_000.0)
    report = agent.execute(
        [OrderIntent("BTCUSDT", 0.20)],
        book,
        {"BTCUSDT": 10_000.0},
        adv={"BTCUSDT": 1e12},
        ts=TS,
        bar_index=1,
    )
    fill = report.fills[0]
    assert fill.price > 10_000.0
    assert fill.fee > 0
    assert book.fees_paid == pytest.approx(fill.fee)
