"""Métriques de performance et de risque, calculées sans arrondi complaisant."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .indicators import annualized_return, drawdown_series, max_drawdown


def _safe(value: float) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return float(value)


def performance(
    equity: pd.Series,
    trades: list[dict] | None = None,
    gross: pd.Series | None = None,
    benchmark: pd.Series | None = None,
    periods_per_year: int = 365,
) -> dict:
    equity = equity.dropna().astype(float)
    out: dict = {}
    if len(equity) < 2:
        return {"error": "historique insuffisant"}

    rets = equity.pct_change().dropna()
    dd = drawdown_series(equity)
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    cagr = annualized_return(equity, periods_per_year)
    vol = float(rets.std(ddof=0) * math.sqrt(periods_per_year))
    downside = rets[rets < 0]
    downside_vol = float(downside.std(ddof=0) * math.sqrt(periods_per_year)) if len(downside) > 2 else 0.0
    mdd = max_drawdown(equity)
    sharpe = float(rets.mean() / rets.std(ddof=0) * math.sqrt(periods_per_year)) if rets.std(ddof=0) > 0 else 0.0
    sortino = float(rets.mean() * periods_per_year / downside_vol) if downside_vol > 0 else 0.0
    calmar = cagr / abs(mdd) if mdd < 0 else 0.0

    # Durée maximale sous le plus haut précédent.
    peak = equity.cummax()
    underwater = equity < peak
    longest = 0
    current_start = None
    for date, flag in underwater.items():
        if flag and current_start is None:
            current_start = date
        elif not flag and current_start is not None:
            longest = max(longest, (date - current_start).days)
            current_start = None
    if current_start is not None:
        longest = max(longest, (equity.index[-1] - current_start).days)
    current_dd_days = (equity.index[-1] - equity[equity == peak].index[-1]).days if (equity == peak).any() else 0

    out.update(
        {
            "start": str(equity.index[0].date()),
            "end": str(equity.index[-1].date()),
            "days": int((equity.index[-1] - equity.index[0]).days),
            "years": float(years),
            "initial_equity": float(equity.iloc[0]),
            "final_equity": float(equity.iloc[-1]),
            "total_return": _safe(total),
            "cagr": _safe(cagr),
            "volatility": _safe(vol),
            "sharpe": _safe(sharpe),
            "sortino": _safe(sortino),
            "calmar": _safe(calmar),
            "max_drawdown": _safe(mdd),
            "max_dd_days": int(longest),
            "current_dd_days": int(current_dd_days),
            "best_day": _safe(float(rets.max())),
            "worst_day": _safe(float(rets.min())),
            "positive_days": _safe(float((rets > 0).mean())),
            "skew": _safe(float(rets.skew())),
            "kurtosis": _safe(float(rets.kurtosis())),
        }
    )
    if gross is not None and len(gross):
        out["avg_gross_exposure"] = _safe(float(gross.mean()))
        out["max_gross_exposure"] = _safe(float(gross.max()))
    if benchmark is not None:
        bench = benchmark.reindex(equity.index).dropna()
        if len(bench) > 2:
            b_rets = bench.pct_change().dropna()
            common = rets.reindex(b_rets.index).dropna()
            b_rets = b_rets.reindex(common.index)
            beta = float(common.cov(b_rets) / b_rets.var()) if b_rets.var() > 0 else 0.0
            corr = float(common.corr(b_rets)) if len(common) > 2 else 0.0
            out["benchmark"] = {
                "total_return": _safe(bench.iloc[-1] / bench.iloc[0] - 1.0),
                "cagr": _safe(annualized_return(bench, periods_per_year)),
                "volatility": _safe(float(b_rets.std(ddof=0) * math.sqrt(periods_per_year))),
                "sharpe": _safe(
                    float(b_rets.mean() / b_rets.std(ddof=0) * math.sqrt(periods_per_year))
                )
                if b_rets.std(ddof=0) > 0
                else 0.0,
                "max_drawdown": _safe(max_drawdown(bench)),
                "beta": _safe(beta),
                "correlation": _safe(corr),
            }
            out["excess_cagr"] = _safe(cagr - annualized_return(bench, periods_per_year))

    if trades:
        fills = trades
        fees = sum(float(t.get("fee", 0.0)) for t in fills)
        slippage_cost = sum(
            float(t.get("notional", 0.0)) * float(t.get("slippage_bps", 0.0)) / 10_000.0
            for t in fills
        )
        sells = [t for t in fills if t.get("side") == "SELL"]
        wins = [float(t.get("pnl", 0.0)) for t in sells if float(t.get("pnl", 0.0)) > 0]
        losses = [float(t.get("pnl", 0.0)) for t in sells if float(t.get("pnl", 0.0)) < 0]
        out.update(
            {
                "fills": len(fills),
                "round_trips": len(sells),
                "fees_paid": _safe(fees),
                "slippage_cost": _safe(slippage_cost),
                "trading_costs": _safe(fees + slippage_cost),
                "hit_rate": _safe(len(wins) / len(sells)) if sells else None,
                "profit_factor": _safe(sum(wins) / abs(sum(losses))) if losses else None,
                "avg_win": _safe(float(np.mean(wins))) if wins else None,
                "avg_loss": _safe(float(np.mean(losses))) if losses else None,
                "expectancy": None,
            }
        )
        all_pnl = wins + losses
        out["expectancy"] = _safe(float(np.mean(all_pnl))) if all_pnl else None
        traded = sum(float(t.get("notional", 0.0)) for t in fills)
        avg_eq = float(equity.mean())
        out["turnover_x"] = _safe(traded / avg_eq / years) if avg_eq > 0 else None
    return out


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    if equity.empty:
        return pd.DataFrame()
    monthly = equity.resample("ME").last().dropna()
    if monthly.empty:
        return pd.DataFrame()
    rets = monthly.pct_change()
    rets.iloc[0] = monthly.iloc[0] / equity.iloc[0] - 1.0
    frame = pd.DataFrame({"return": rets})
    frame["year"] = frame.index.year
    frame["month"] = frame.index.month
    return frame


def yearly_returns(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    yearly = equity.resample("YE").last().dropna()
    rets = yearly.pct_change()
    if len(yearly):
        rets.iloc[0] = yearly.iloc[0] / equity.iloc[0] - 1.0
    return rets


def rolling_sharpe(equity: pd.Series, window: int = 90, periods_per_year: int = 365) -> pd.Series:
    rets = equity.pct_change()
    mean = rets.rolling(window, min_periods=window // 2).mean()
    std = rets.rolling(window, min_periods=window // 2).std(ddof=0)
    return mean / std.replace(0, np.nan) * math.sqrt(periods_per_year)


def format_metrics(metrics: dict, benchmark_name: str = "BTC buy & hold") -> str:
    def pct(key: str) -> str:
        value = metrics.get(key)
        return "n/a" if value is None else f"{value * 100:+.1f}%"

    def num(key: str, digits: int = 2) -> str:
        value = metrics.get(key)
        return "n/a" if value is None else f"{value:.{digits}f}"

    lines = [
        f"Période            : {metrics.get('start')} → {metrics.get('end')} "
        f"({metrics.get('years', 0):.1f} ans)",
        f"Equity finale      : {metrics.get('final_equity', 0):,.0f} $".replace(",", " "),
        f"Rendement total    : {pct('total_return')}",
        f"CAGR               : {pct('cagr')}",
        f"Volatilité         : {pct('volatility')}",
        f"Sharpe             : {num('sharpe')}",
        f"Sortino            : {num('sortino')}",
        f"Calmar             : {num('calmar')}",
        f"Max drawdown       : {pct('max_drawdown')}",
        f"Jours sous l'eau   : {metrics.get('max_dd_days', 0)}",
        f"Exposition moyenne : {pct('avg_gross_exposure')}",
        f"Turnover annuel    : {num('turnover_x', 1)}x",
        f"Frais              : {metrics.get('fees_paid', 0):,.0f} $".replace(",", " "),
        f"Coûts totaux       : {metrics.get('trading_costs', 0):,.0f} $".replace(",", " "),
        f"Trades clôturés    : {metrics.get('round_trips', 0)}",
        f"Hit rate           : {pct('hit_rate')}",
        f"Profit factor      : {num('profit_factor')}",
    ]
    bench = metrics.get("benchmark")
    if bench:
        lines.append(
            f"{benchmark_name:<19}: {pct('excess_cagr')} vs benchmark, "
            f"Sharpe {bench.get('sharpe', 0):.2f}, DD {bench.get('max_drawdown', 0) * 100:.1f}%"
        )
    return "\n".join(lines)
