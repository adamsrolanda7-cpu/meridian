"""Les indicateurs ne doivent jamais regarder la bougie courante ni le futur."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from meridian import indicators as ta


@pytest.fixture()
def btc(panel):
    return panel["BTCUSDT"]


def test_ema_causal(btc):
    cut = len(btc) - 40
    full = ta.ema(btc["close"], 50)
    trunc = ta.ema(btc["close"].iloc[:cut], 50)
    assert full.iloc[cut - 1] == pytest.approx(trunc.iloc[cut - 1], rel=1e-12)


def test_atr_rsi_adx_causal(btc):
    cut = len(btc) - 40
    trunc = btc.iloc[:cut]
    pairs = [
        (ta.atr(btc, 14), ta.atr(trunc, 14)),
        (ta.rsi(btc["close"], 14), ta.rsi(trunc["close"], 14)),
    ]
    full_adx = ta.adx(btc, 14)
    trunc_adx = ta.adx(trunc, 14)
    pairs += list(zip(full_adx, trunc_adx))
    for full, short in pairs:
        assert full.iloc[cut - 1] == pytest.approx(short.iloc[cut - 1], rel=1e-12, nan_ok=True)


def test_donchian_excludes_current_bar(btc):
    upper, lower = ta.donchian(btc, 20)
    window = btc["high"].iloc[:20]
    assert upper.iloc[20] == pytest.approx(window.max())
    assert lower.iloc[20] == pytest.approx(btc["low"].iloc[:20].min())


def test_zscore_and_vol_causal(btc):
    cut = len(btc) - 30
    trunc = btc.iloc[:cut]
    assert ta.zscore(btc["close"], 20).iloc[cut - 1] == pytest.approx(
        ta.zscore(trunc["close"], 20).iloc[cut - 1]
    )
    returns = btc["close"].pct_change()
    short_returns = trunc["close"].pct_change()
    assert ta.realized_vol(returns, 30).iloc[cut - 1] == pytest.approx(
        ta.realized_vol(short_returns, 30).iloc[cut - 1]
    )


def test_max_drawdown_known_values():
    equity = pd.Series([100.0, 120.0, 60.0, 90.0, 150.0])
    assert ta.max_drawdown(equity) == pytest.approx(-0.5)


def test_rsi_extremes():
    up = pd.Series(np.linspace(1.0, 2.0, 100))
    down = pd.Series(np.linspace(2.0, 1.0, 100))
    assert ta.rsi(up, 14).iloc[-1] == pytest.approx(100.0)
    assert ta.rsi(down, 14).iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_rolling_percentile_is_causal(btc):
    returns = btc["close"].pct_change()
    vol = ta.realized_vol(returns, 30)
    cut = len(vol) - 20
    full = ta.rolling_percentile(vol, 120)
    trunc = ta.rolling_percentile(vol.iloc[:cut], 120)
    assert full.iloc[cut - 1] == pytest.approx(trunc.iloc[cut - 1])
