"""Indicateurs techniques causaux, sans dépendance externe.

Toutes les fonctions ne regardent que le passé de la série : les colonnes
produites à la date t n'utilisent que les données <= t. Le backtest exécute
ensuite les décisions à l'ouverture t+1.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # RSI = 100 quand il n'y a eu que des hausses sur la fenêtre.
    out = out.where(avg_loss > 0, 100.0)
    out = out.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return out


def adx(df: pd.DataFrame, window: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Retourne (ADX, +DI, -DI) en lissage de Wilder."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index
    )
    atr_w = atr(df, window).replace(0.0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean() / atr_w
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean() / atr_w
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_w = dx.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    return adx_w, plus_di, minus_di


def bollinger(series: pd.Series, window: int = 20, k: float = 2.0):
    mid = sma(series, window)
    std = series.rolling(window, min_periods=window).std(ddof=0)
    return mid, mid + k * std, mid - k * std, std


def donchian(df: pd.DataFrame, window: int = 55) -> tuple[pd.Series, pd.Series]:
    """Canal de Donchian calculé sur les bougies *précédentes* seulement."""
    upper = df["high"].rolling(window, min_periods=window).max().shift(1)
    lower = df["low"].rolling(window, min_periods=window).min().shift(1)
    return upper, lower


def zscore(series: pd.Series, window: int = 20) -> pd.Series:
    mean = sma(series, window)
    std = series.rolling(window, min_periods=window).std(ddof=0)
    return (series - mean) / std.replace(0.0, np.nan)


def realized_vol(returns: pd.Series, window: int, periods_per_year: int = 365) -> pd.Series:
    return returns.rolling(window, min_periods=max(5, window // 3)).std(ddof=0) * np.sqrt(
        periods_per_year
    )


def rolling_percentile(series: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    min_periods = min_periods or max(20, window // 3)

    def last_rank(arr: np.ndarray) -> float:
        value = arr[-1]
        if np.isnan(value):
            return np.nan
        valid = arr[~np.isnan(arr)]
        if len(valid) < min_periods:
            return np.nan
        return float((valid <= value).mean())

    return series.rolling(window, min_periods=min_periods).apply(last_rank, raw=True)


def max_drawdown(equity: pd.Series | np.ndarray) -> float:
    values = np.asarray(equity, dtype=float)
    if values.size == 0:
        return 0.0
    peak = np.maximum.accumulate(values)
    return float((values / peak - 1.0).min())


def drawdown_series(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0


def annualized_return(equity: pd.Series, periods_per_year: int = 365) -> float:
    values = equity.dropna()
    if len(values) < 2 or values.iloc[0] <= 0:
        return 0.0
    years = (values.index[-1] - values.index[0]).days / 365.25
    if years <= 0:
        return 0.0
    return float((values.iloc[-1] / values.iloc[0]) ** (1.0 / years) - 1.0)


def sharpe(returns: pd.Series, periods_per_year: int = 365) -> float:
    r = returns.dropna()
    if len(r) < 5 or r.std(ddof=0) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=0) * np.sqrt(periods_per_year))
