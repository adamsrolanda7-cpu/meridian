"""Comptabilité du book : cash, frais, PnL, sérialisation."""
from __future__ import annotations

import pandas as pd
import pytest

from meridian.portfolio import Book


TS = pd.Timestamp("2026-01-01", tz="UTC")


def test_buy_sell_accounting():
    book = Book(10_000.0)
    book.buy(TS, "BTCUSDT", 1.0, 100.0, fee=1.0, stop=90.0, sleeve="trend")
    assert book.cash == pytest.approx(9_899.0)
    assert book.positions["BTCUSDT"].qty == pytest.approx(1.0)
    assert book.positions["BTCUSDT"].avg_price == pytest.approx(100.0)
    book.mark({"BTCUSDT": 110.0})
    assert book.equity() == pytest.approx(9_899.0 + 110.0)
    assert book.unrealized() == pytest.approx(10.0)

    book.buy(TS, "BTCUSDT", 1.0, 200.0, fee=1.0, stop=180.0)
    pos = book.positions["BTCUSDT"]
    assert pos.qty == pytest.approx(2.0)
    assert pos.avg_price == pytest.approx(150.0)
    assert pos.stop == pytest.approx(180.0)  # le stop ne descend pas

    fill = book.sell(TS, "BTCUSDT", 1.0, 300.0, fee=1.0, reason="test")
    assert fill.realized_pnl == pytest.approx((300.0 - 150.0) * 1.0 - 1.0)
    assert book.realized_pnl == pytest.approx(149.0)
    assert book.positions["BTCUSDT"].qty == pytest.approx(1.0)
    assert book.fees_paid == pytest.approx(3.0)
    assert book.cash == pytest.approx(9_899.0 - 201.0 + 299.0)


def test_sell_all_closes_position():
    book = Book(1_000.0)
    book.buy(TS, "ETHUSDT", 0.5, 1_000.0, fee=0.5)
    book.sell(TS, "ETHUSDT", 1.0, 1_100.0, fee=0.5)  # sur-vente bornée
    assert "ETHUSDT" not in book.positions
    assert book.cash == pytest.approx(1_000.0 - 500.5 + 549.5)


def test_insufficient_cash_raises():
    book = Book(100.0)
    with pytest.raises(ValueError):
        book.buy(TS, "BTCUSDT", 1.0, 100.0, fee=1.0)


def test_weights_and_snapshot():
    book = Book(10_000.0)
    book.buy(TS, "BTCUSDT", 0.1, 10_000.0, fee=2.0)
    book.mark({"BTCUSDT": 10_000.0})
    weights = book.weights()
    assert sum(weights.values()) == pytest.approx(book.gross_exposure())
    snap = book.snapshot()
    assert snap["equity"] == pytest.approx(book.equity())
    assert snap["positions"]["BTCUSDT"]["qty"] == pytest.approx(0.1)


def test_book_serialization_roundtrip():
    book = Book(5_000.0)
    book.buy(TS, "SOLUSDT", 10.0, 20.0, fee=0.2, stop=15.0, sleeve="meanrev")
    book.mark({"SOLUSDT": 25.0})
    raw = book.to_dict()
    restored = Book.from_dict(raw)
    assert restored.cash == pytest.approx(book.cash)
    assert restored.fees_paid == pytest.approx(book.fees_paid)
    assert restored.positions["SOLUSDT"].qty == pytest.approx(10.0)
    assert restored.positions["SOLUSDT"].stop == pytest.approx(15.0)
    assert restored.positions["SOLUSDT"].sleeve == "meanrev"
    assert restored.equity() == pytest.approx(book.equity())
