"""Moteur de backtest événementiel, sans look-ahead.

Timeline d'une bougie quotidienne :
1. ouverture : stops gap + exécution des intentions décidées la veille ;
2. intrabar : stops sur les positions détenues avant la séance ;
3. clôture : mark-to-market, trailing stops, gouverneur de risque ;
4. décision : régime, signaux, allocation, veto risque → intentions pour
   l'ouverture suivante.

Les agents sont identiques en backtest et en paper : seule la source de prix
change. C'est le test de cohérence du système.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .agents.execution import ExecutionAgent, ExecutionReport
from .agents.portfolio import Allocation, PortfolioAgent
from .agents.regime import RegimeAgent
from .agents.risk import Governor, RiskAgent
from .agents.signals import CrossSectionalAgent, MeanReversionAgent, TrendAgent
from .config import get
from .features import FeatureStore
from .metrics import performance
from .portfolio import Book
from .types import Decision, OrderIntent, Regime, Signal, Target


@dataclass
class BacktestResult:
    equity: pd.Series
    benchmark: pd.Series
    gross: pd.Series
    trades: list[dict]
    decisions: list[dict]
    metrics: dict
    book: Book
    latest_decision: Decision | None
    latest_execution: ExecutionReport | None
    governor: Governor | None
    start: str
    end: str
    assets: list[str]
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "assets": self.assets,
            "metrics": self.metrics,
            "book": self.book.snapshot(),
            "latest_decision": self.latest_decision.as_dict() if self.latest_decision else None,
            "diagnostics": self.diagnostics,
        }


def ewma_cov_series(store: FeatureStore, lam: float = 0.97) -> list[np.ndarray]:
    """Covariance EWMA causale pour chaque date (matrices k×k)."""
    assets = store.assets
    k = len(assets)
    rets = np.column_stack(
        [store.frames[a]["ret1"].reindex(store.dates).to_numpy(dtype=float) for a in assets]
    )
    cov = np.zeros((k, k))
    out: list[np.ndarray] = []
    for i in range(len(store.dates)):
        r = rets[i]
        active = np.isfinite(r)
        cov *= lam
        if active.any():
            idx = np.where(active)[0]
            block = cov[np.ix_(idx, idx)] + (1.0 - lam) * np.outer(r[idx], r[idx])
            cov[np.ix_(idx, idx)] = block
        out.append(cov.copy())
    return out


def _safe(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if np.isfinite(value) else 0.0


def _regime_at(frame: pd.DataFrame, i: int, date: pd.Timestamp) -> Regime:
    row = frame.iloc[i]
    return Regime(
        label=str(row["label"]),
        score=_safe(row["score"]),
        confidence=_safe(row["confidence"]),
        breadth=_safe(row["breadth"]),
        vol_pct=_safe(row["vol_pct"]),
        adx=_safe(row["btc_adx"]),
        reason=str(row["reason"]),
    )


class Backtest:
    def __init__(self, store: FeatureStore, cfg: dict | None = None, cov_series=None):
        self.store = store
        self.cfg = cfg or store.cfg
        self.trend = TrendAgent(self.cfg)
        self.meanrev = MeanReversionAgent(self.cfg)
        self.xsmom = CrossSectionalAgent(self.cfg)
        self.regime_agent = RegimeAgent(self.cfg)
        self.trend.compute(store)
        self.meanrev.compute(store)
        self.xsmom.compute(store)
        self.regime_agent.compute(store)
        self.trend.attach(store)
        self.cov = cov_series or ewma_cov_series(
            store, float(get(self.cfg, "strategy.cov_lambda", 0.97))
        )
        # Tableaux numpy : le hot loop ne doit pas passer par pandas.
        self.signal_arrays = {
            "trend": {
                a: (
                    self.trend.frames[a]["score"].to_numpy(dtype=float),
                    self.trend.frames[a]["confidence"].to_numpy(dtype=float),
                )
                for a in store.assets
            },
            "meanrev": {
                a: (
                    self.meanrev.frames[a]["score"].to_numpy(dtype=float),
                    self.meanrev.frames[a]["confidence"].to_numpy(dtype=float),
                )
                for a in store.assets
            },
            "xsmom": {
                a: (
                    self.xsmom.frames[a]["score"].to_numpy(dtype=float),
                    self.xsmom.frames[a]["confidence"].to_numpy(dtype=float),
                )
                for a in store.assets
            },
        }
        self.regime_np = {
            column: store.regime[column].to_numpy()
            for column in ("label", "score", "confidence", "breadth", "vol_pct", "btc_adx", "reason")
        }

    # ------------------------------------------------------------------ helpers
    def _col(self, asset: str, name: str) -> np.ndarray:
        return self.store.frames[asset][name].reindex(self.store.dates).to_numpy(dtype=float)

    def _start_index(self, start: pd.Timestamp | None) -> int:
        dates = self.store.dates
        idx = int(np.searchsorted(dates, start)) if start is not None else 0
        idx = max(idx, self.store.warmup_index())
        labels = self.store.regime["label"].to_numpy()
        while idx < len(dates) and labels[idx] == "WARMUP":
            idx += 1
        return idx

    def _end_index(self, end: pd.Timestamp | None) -> int:
        if end is None:
            return len(self.store.dates) - 1
        return int(np.searchsorted(self.store.dates, end, side="right")) - 1

    def _scores(self, i: int) -> dict[str, dict[str, tuple[float, float]]]:
        out: dict[str, dict[str, tuple[float, float]]] = {}
        for asset in self.store.assets:
            out[asset] = {}
            for sleeve in ("trend", "meanrev", "xsmom"):
                score, conf = self.signal_arrays[sleeve][asset]
                out[asset][sleeve] = (_safe(score[i]), _safe(conf[i]))
        return out

    def _regime_fast(self, i: int) -> Regime:
        return Regime(
            label=str(self.regime_np["label"][i]),
            score=_safe(self.regime_np["score"][i]),
            confidence=_safe(self.regime_np["confidence"][i]),
            breadth=_safe(self.regime_np["breadth"][i]),
            vol_pct=_safe(self.regime_np["vol_pct"][i]),
            adx=_safe(self.regime_np["btc_adx"][i]),
            reason=str(self.regime_np["reason"][i]),
        )

    def _signal_objects(self, date: pd.Timestamp) -> list[Signal]:
        signals: list[Signal] = []
        for asset in self.store.assets:
            signals.append(self.trend.signal(asset, date))
            signals.append(self.meanrev.signal(asset, date))
            signals.append(self.xsmom.signal(asset, date))
        return signals

    # ----------------------------------------------------------------------- run
    def run(
        self,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        initial_equity: float | None = None,
        cost_multiplier: float = 1.0,
        record_decisions: bool = True,
    ) -> BacktestResult:
        cfg = self.cfg
        if isinstance(start, str):
            start = pd.Timestamp(start, tz="UTC")
        if isinstance(end, str):
            end = pd.Timestamp(end, tz="UTC")
        start_i = self._start_index(start)
        end_i = self._end_index(end)
        if end_i < start_i:
            raise ValueError("fenêtre de backtest vide")
        dates = self.store.dates[start_i : end_i + 1]
        initial = float(initial_equity or cfg.get("initial_equity", 100_000.0))
        book = Book(initial)
        risk = RiskAgent(cfg)
        allocator = PortfolioAgent(cfg)
        executor = ExecutionAgent(cfg)
        executor.fee_bps *= cost_multiplier
        executor.slip_bps *= cost_multiplier
        executor.impact_bps *= cost_multiplier

        prices = {
            a: {
                "open": self._col(a, "open"),
                "high": self._col(a, "high"),
                "low": self._col(a, "low"),
                "close": self._col(a, "close"),
                "atr": self._col(a, "atr"),
                "vol30": self._col(a, "vol30"),
                "adv20": self._col(a, "adv20"),
            }
            for a in self.store.assets
        }
        regime_labels = self.store.regime["label"].to_numpy()
        equity_rows: list[tuple[pd.Timestamp, float]] = []
        gross_rows: list[tuple[pd.Timestamp, float]] = []
        decisions: list[dict] = []
        pending: list[OrderIntent] = []
        last_allocation: Allocation | None = None
        last_targets: list[Target] = []
        last_governor: Governor | None = None
        last_execution: ExecutionReport | None = None
        last_decision: Decision | None = None
        last_prices: dict[str, float] = {}
        rebalance_days = max(1, int(get(cfg, "strategy.rebalance_days", 7)))
        min_hold = max(0, int(get(cfg, "strategy.min_hold_days", 5)))
        prev_regime_label: str | None = None
        prev_scale: float | None = None
        halt_dates: list[str] = []
        halted_prev = False

        for i in range(start_i, end_i + 1):
            date = self.store.dates[i]
            open_prices: dict[str, float] = {}
            close_prices: dict[str, float] = {}
            for asset in self.store.assets:
                op = prices[asset]["open"][i]
                cl = prices[asset]["close"][i]
                if np.isfinite(op) and op > 0:
                    open_prices[asset] = float(op)
                if np.isfinite(cl) and cl > 0:
                    close_prices[asset] = float(cl)

            # 1) stops en gap à l'ouverture
            for asset in list(book.positions):
                pos = book.positions[asset]
                if pos.entry_index >= i or asset not in open_prices:
                    continue
                px = open_prices[asset]
                if px <= pos.stop:
                    slip = executor.slip_bps + executor._impact(pos.qty * px, prices[asset]["adv20"][i])
                    fill_price = px * (1.0 - slip / 10_000.0)
                    fee = pos.qty * fill_price * executor.fee_bps / 10_000.0
                    book.sell(date, asset, pos.qty, fill_price, fee, "stop gap", slip)
                    risk.set_cooldown(asset, i)

            # 2) exécution des intentions de la veille
            if pending:
                blocked = risk.blocked(i)
                adjusted: list[OrderIntent] = []
                for intent in pending:
                    target = 0.0 if intent.asset in blocked else intent.target_weight
                    adjusted.append(OrderIntent(intent.asset, target, intent.reason))
                atrs = {}
                sleeves = {}
                for intent in adjusted:
                    atrs[intent.asset] = prices[intent.asset]["atr"][max(0, i - 1)]
                    sleeve = "trend"
                    if last_allocation and intent.asset in last_allocation.sleeves:
                        sleeve = last_allocation.sleeves[intent.asset]
                    sleeves[intent.asset] = sleeve
                stop_levels = {
                    a: risk.entry_stop(
                        open_prices[a],
                        atrs.get(a, np.nan),
                        sleeves.get(a, "trend"),
                    )
                    for a in open_prices
                    if a in {x.asset for x in adjusted}
                }
                adv = {a: prices[a]["adv20"][i] for a in self.store.assets}
                last_execution = executor.execute(
                    adjusted,
                    book,
                    open_prices,
                    adv,
                    date,
                    i,
                    stops=stop_levels,
                    sleeves=sleeves,
                )
                pending = []

            # 3) stops intrabar (mean-reversion uniquement ; les sleeves de
            # tendance utilisent un stop sur clôture pour éviter les mèches).
            # Un hard stop catastrophique optionnel protège les sleeves de
            # tendance contre les gaps/effondrements que le stop 6 ATR sur
            # clôture ne voit pas.
            for asset in list(book.positions):
                pos = book.positions[asset]
                if pos.entry_index >= i:
                    continue
                low = prices[asset]["low"][i]
                if not np.isfinite(low):
                    continue
                if pos.sleeve in ("trend", "xsmom"):
                    if risk.hard_stop_pct > 0 and asset in open_prices:
                        hard = pos.avg_price * (1.0 - risk.hard_stop_pct)
                        if low <= hard:
                            px = min(open_prices[asset], hard)
                            slip = executor.slip_bps + executor._impact(pos.qty * px, prices[asset]["adv20"][i])
                            fill_price = px * (1.0 - slip / 10_000.0)
                            fee = pos.qty * fill_price * executor.fee_bps / 10_000.0
                            book.sell(date, asset, pos.qty, fill_price, fee, "hard stop", slip)
                            risk.set_cooldown(asset, i)
                    continue
                if low <= pos.stop and asset in open_prices:
                    px = min(open_prices[asset], pos.stop)
                    slip = executor.slip_bps + executor._impact(pos.qty * px, prices[asset]["adv20"][i])
                    fill_price = px * (1.0 - slip / 10_000.0)
                    fee = pos.qty * fill_price * executor.fee_bps / 10_000.0
                    book.sell(date, asset, pos.qty, fill_price, fee, "stop ATR", slip)
                    risk.set_cooldown(asset, i)

            # 4) mark-to-market, âge des positions, trailing stops
            book.mark(close_prices)
            for asset, pos in book.positions.items():
                if pos.entry_index < i:
                    pos.bars_held += 1
                    risk.update_trailing(pos, close_prices.get(asset, pos.last_price), prices[asset]["atr"][i])

            # Stops sur clôture pour la tendance : moins de bruit que les mèches.
            for asset in list(book.positions):
                pos = book.positions[asset]
                if pos.entry_index >= i or pos.sleeve not in ("trend", "xsmom"):
                    continue
                close = close_prices.get(asset)
                if close is None:
                    continue
                if close <= pos.stop:
                    slip = executor.slip_bps + executor._impact(pos.qty * close, prices[asset]["adv20"][i])
                    fill_price = close * (1.0 - slip / 10_000.0)
                    fee = pos.qty * fill_price * executor.fee_bps / 10_000.0
                    book.sell(date, asset, pos.qty, fill_price, fee, "stop close", slip)
                    risk.set_cooldown(asset, i)

            equity = book.equity()
            last_prices = dict(close_prices)
            equity_rows.append((date, equity))
            gross_rows.append((date, book.gross_exposure()))

            # 5) décision. Le portefeuille est rééquilibré chaque semaine, ou
            # immédiatement si le régime, le gouverneur ou une sortie MR change.
            regime = self._regime_fast(i)
            governor = risk.update(equity, i)
            last_governor = governor
            if governor.halted and not halted_prev:
                halt_dates.append(str(date.date()))
            halted_prev = governor.halted
            mr_exit: set[str] = set()
            for asset, pos in book.positions.items():
                if pos.sleeve == "meanrev" and self.meanrev.should_exit(asset, date, pos.bars_held):
                    mr_exit.add(asset)

            regime_changed = prev_regime_label is not None and regime.label != prev_regime_label
            scale_changed = prev_scale is not None and abs(governor.scale - prev_scale) > 1e-9
            due = (
                (i - start_i) % rebalance_days == 0
                or regime_changed
                or scale_changed
                or bool(mr_exit)
                or i == end_i
            )
            prev_regime_label = regime.label
            prev_scale = governor.scale

            if due:
                current_w = book.weights(close_prices)
                scores = self._scores(i)
                vols = {a: _safe(prices[a]["vol30"][i]) for a in self.store.assets}
                adv = {a: _safe(prices[a]["adv20"][i]) for a in self.store.assets}
                allocation = allocator.build(
                    date=date,
                    regime=regime,
                    scores=scores,
                    vols=vols,
                    cov=self.cov[i],
                    assets=self.store.assets,
                    current=current_w,
                    equity=equity,
                    adv=adv,
                    mr_exit=mr_exit,
                    blocked=risk.blocked(i),
                )
                targets, risk_notes = risk.apply(allocation.targets, governor, current_w, i)
                last_allocation = allocation
                last_targets = targets
                if not np.isfinite(allocation.portfolio_vol):
                    allocation.portfolio_vol = 0.0

                intents: list[OrderIntent] = []
                for target in targets:
                    cur = current_w.get(target.asset, 0.0)
                    if target.weight <= 1e-9 and cur <= 1e-9:
                        continue
                    # Durée minimale de détention : on ne sort pas sur un
                    # simple clignotement de score, sauf stop/risk-off.
                    if target.weight <= 1e-9 and cur > 1e-9:
                        pos = book.positions.get(target.asset)
                        if (
                            pos is not None
                            and pos.bars_held < min_hold
                            and governor.scale > 0
                            and regime.label not in ("TREND_DOWN", "CRISIS")
                        ):
                            continue
                    if target.weight > 1e-9 and abs(target.weight - cur) < 0.01:
                        continue
                    if target.weight <= 1e-9 and cur <= 1e-9:
                        continue
                    intents.append(OrderIntent(target.asset, target.weight, target.reason))
                pending = intents
            else:
                pending = []
                allocation = last_allocation
                targets = last_targets
                risk_notes = []

            if record_decisions and due:
                decisions.append(
                    {
                        "date": str(date.date()),
                        "regime": regime.label,
                        "regime_confidence": round(regime.confidence, 3),
                        "gross_target": round(sum(t.weight for t in targets), 4),
                        "portfolio_vol": round(allocation.portfolio_vol if allocation else 0.0, 4),
                        "equity": round(equity, 2),
                        "drawdown": round(governor.drawdown, 4),
                        "targets": {
                            t.asset: round(t.weight, 4) for t in targets if t.weight > 1e-9
                        },
                        "orders": [
                            {"asset": o.asset, "target": round(o.target_weight, 4), "reason": o.reason}
                            for o in intents
                        ],
                        "risk_notes": risk_notes[-4:],
                    }
                )

        # Dernière décision complète (memo / audit)
        if last_targets:
            last_regime = self._regime_fast(end_i)
            last_decision = Decision(
                ts=self.store.dates[end_i],
                regime=last_regime,
                signals=self._signal_objects(self.store.dates[end_i]),
                targets=last_targets,
                orders=pending,
                risk_notes=(last_governor.notes if last_governor else []),
                gross_target=float(sum(t.weight for t in last_targets)),
                portfolio_vol=float(last_allocation.portfolio_vol if last_allocation else 0.0),
                equity=float(equity_rows[-1][1]) if equity_rows else initial,
            )
        elif last_allocation:
            last_decision = Decision(
                ts=self.store.dates[end_i],
                regime=self._regime_fast(end_i),
                signals=self._signal_objects(self.store.dates[end_i]),
                targets=last_allocation.targets,
                gross_target=last_allocation.gross,
                portfolio_vol=last_allocation.portfolio_vol,
                equity=float(equity_rows[-1][1]) if equity_rows else initial,
            )

        equity = pd.Series(
            [v for _, v in equity_rows], index=pd.DatetimeIndex([d for d, _ in equity_rows]), name="equity"
        )
        gross = pd.Series(
            [v for _, v in gross_rows], index=equity.index, name="gross"
        )
        btc = self.store.frames["BTCUSDT"]["close"].reindex(equity.index)
        benchmark = initial * btc / btc.iloc[0]

        metrics = performance(
            equity,
            trades=book.trades,
            gross=gross,
            benchmark=benchmark,
            periods_per_year=365,
        )
        diagnostics = {
            "trades": len(book.trades),
            "final_cash": book.cash,
            "fees_paid": book.fees_paid,
            "realized_pnl": book.realized_pnl,
            "unrealized_pnl": book.unrealized(),
            "governor": {
                "peak": risk.peak,
                "prev_equity": risk.prev_equity,
                "halted": risk.halted,
                "cooldowns": risk.cooldowns,
            },
            "last_prices": last_prices,
            "risk_history": risk.history[-1] if risk.history else None,
            "halt_dates": halt_dates,
        }

        return BacktestResult(
            equity=equity,
            benchmark=benchmark,
            gross=gross,
            trades=book.trades,
            decisions=decisions,
            metrics=metrics,
            book=book,
            latest_decision=last_decision,
            latest_execution=last_execution,
            governor=last_governor,
            start=str(equity.index[0].date()),
            end=str(equity.index[-1].date()),
            assets=self.store.assets,
            diagnostics=diagnostics,
        )
