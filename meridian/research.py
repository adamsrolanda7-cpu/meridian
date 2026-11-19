"""Recherche et validation : IS/OOS, walk-forward, Monte Carlo, stress de coûts.

La règle du desk : on choisit sur l'in-sample, on ne touche plus aux paramètres,
et on publie l'out-of-sample tel quel — même s'il est décevant.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .agents.signals import CrossSectionalAgent, MeanReversionAgent, TrendAgent
from .backtest import Backtest, BacktestResult
from .config import ROOT, deep_merge, get, load_config
from .data import ensure_data, quality_report
from .features import FeatureStore
from .metrics import performance

RUNS = os.path.join(ROOT, "runs")


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _metrics_brief(m: dict) -> dict:
    keys = (
        "cagr",
        "volatility",
        "sharpe",
        "sortino",
        "calmar",
        "max_drawdown",
        "avg_gross_exposure",
        "turnover_x",
        "hit_rate",
        "profit_factor",
        "total_return",
        "round_trips",
    )
    return {k: m.get(k) for k in keys}


def _score(m: dict) -> float:
    sharpe = m.get("sharpe") or -99.0
    dd = m.get("max_drawdown") or -1.0
    turnover = m.get("turnover_x") or 99.0
    if m.get("round_trips", 0) < 10 or dd < -0.22 or turnover > 9.0:
        return -99.0 + sharpe
    return sharpe + 0.25 * min(2.0, abs(sharpe) if sharpe > 0 else 0.0)


def window_metrics(
    equity: pd.Series,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp | None = None,
    fresh: bool = True,
    initial: float = 100_000.0,
) -> dict:
    """Mesure une fenêtre de la courbe d'equity sans mélanger deux notions.

    ``fresh=True`` : capital neuf au début de la fenêtre. C'est l'OOS « nouveau
    compte », celui que vivrait un allocateur qui arrive à cette date.

    ``fresh=False`` : les rendements de la fenêtre sont enchaînés depuis la
    courbe complète, et le drawdown est mesuré depuis le plus haut historique.
    C'est l'OOS « continu », celui d'un compte déjà investi au début de la
    fenêtre. Les deux diffèrent : le premier doit ré-entrer en position, le
    second porte les positions existantes à travers la coupure.
    """
    rets = equity.pct_change().dropna()
    rets = rets.loc[start:end]
    if rets.empty:
        return {}
    eq = (1.0 + rets).cumprod() * float(initial)
    metrics = performance(eq)
    metrics["initial_equity"] = float(initial)
    if not fresh:
        last = end if end is not None else equity.index[-1]
        hist = equity.loc[:last]
        peak = hist.cummax()
        window = hist.loc[rets.index]
        drawdown = window / peak.loc[rets.index] - 1.0
        metrics["max_drawdown"] = float(drawdown.min())
        metrics["dd_at_end"] = float(drawdown.iloc[-1])
    return metrics


@dataclass
class ResearchReport:
    started_at: str
    elapsed_sec: float
    config: dict
    split: dict
    grid: list[dict]
    selected: dict
    oos: dict
    walkforward: dict
    montecarlo: dict
    cost_stress: list[dict]
    start_dates: list[dict]
    sensitivity: dict
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _jsonable(
            {
                "started_at": self.started_at,
                "elapsed_sec": self.elapsed_sec,
                "config": self.config,
                "split": self.split,
                "grid": self.grid,
                "selected": self.selected,
                "oos": self.oos,
                "walkforward": self.walkforward,
                "montecarlo": self.montecarlo,
                "cost_stress": self.cost_stress,
                "start_dates": self.start_dates,
                "sensitivity": self.sensitivity,
                "notes": self.notes,
            }
        )


def split_dates(store: FeatureStore, ratio: float = 0.70) -> tuple[str, str]:
    start = store.dates[store.warmup_index()]
    end = store.dates[-1]
    cut = start + (end - start) * ratio
    return str(start.date()), str(cut.date())


def monte_carlo(equity: pd.Series, paths: int = 1000, block: int = 20, seed: int = 11) -> dict:
    rets = equity.pct_change().dropna().to_numpy()
    if len(rets) < block * 2:
        return {}
    rng = np.random.default_rng(seed)
    n = len(rets)
    n_blocks = int(math.ceil(n / block))
    cagrs, dds, sharpes, totals = [], [], [], []
    for _ in range(paths):
        idx = rng.integers(0, n - block, size=n_blocks)
        sample = np.concatenate([rets[i : i + block] for i in idx])[:n]
        eq = np.cumprod(1.0 + sample)
        total = eq[-1] - 1.0
        years = n / 365.25
        cagr = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
        peak = np.maximum.accumulate(eq)
        dd = float((eq / peak - 1.0).min())
        vol = float(sample.std(ddof=0) * math.sqrt(365))
        sharpe = float(sample.mean() / sample.std(ddof=0) * math.sqrt(365)) if sample.std(ddof=0) > 0 else 0.0
        cagrs.append(cagr)
        dds.append(dd)
        sharpes.append(sharpe)
        totals.append(total)
    arr = lambda x: np.array(x)
    return {
        "paths": paths,
        "block_days": block,
        "cagr_p5": float(np.percentile(arr(cagrs), 5)),
        "cagr_p50": float(np.percentile(arr(cagrs), 50)),
        "cagr_p95": float(np.percentile(arr(cagrs), 95)),
        "maxdd_p5": float(np.percentile(arr(dds), 5)),
        "maxdd_p50": float(np.percentile(arr(dds), 50)),
        "maxdd_p95": float(np.percentile(arr(dds), 95)),
        "sharpe_p5": float(np.percentile(arr(sharpes), 5)),
        "sharpe_p50": float(np.percentile(arr(sharpes), 50)),
        "sharpe_p95": float(np.percentile(arr(sharpes), 95)),
        "prob_negative": float(np.mean(arr(totals) < 0)),
        "prob_dd_worse_20": float(np.mean(arr(dds) < -0.20)),
        "_cagrs": arr(cagrs),
        "_dds": arr(dds),
    }


def _fold_ranges(
    start: pd.Timestamp,
    end: pd.Timestamp,
    folds: int,
    test_days: int = 365,
) -> list[tuple[str, str]]:
    """Fenêtres de test contiguës, ancrées sur la fin de l'historique.

    Chaque fenêtre est un test ``test_days`` jours après un train expansif.
    """
    out = []
    for i in range(folds):
        test_start = end - pd.Timedelta(days=test_days * (folds - i)) + pd.Timedelta(days=1)
        test_end = test_start + pd.Timedelta(days=test_days - 1)
        if test_start <= start:
            continue
        out.append((str(test_start.date()), str(min(test_end, end).date())))
    return out


def run_research(
    cfg: dict | None = None,
    panel: dict | None = None,
    quick: bool = False,
    paths: int = 800,
) -> ResearchReport:
    t0 = time.time()
    cfg = cfg or load_config()
    if panel is None:
        panel, source = ensure_data(cfg, quiet=False)
    store = FeatureStore.build(panel, cfg)
    engine = Backtest(store, cfg)
    is_start, split_date = split_dates(store, float(get(cfg, "research.split_ratio", 0.70)))
    full = engine.run()
    is_res = engine.run(start=is_start, end=split_date)
    oos_res = engine.run(start=str((pd.Timestamp(split_date) + pd.Timedelta(days=1)).date()))

    # Grille : uniquement des paramètres de risque/portefeuille, pas les
    # formules alpha. On évite de transformer la recherche en machine à
    # sur-ajuster le signal.
    grid_spec = {
        "strategy.target_vol": [0.22, 0.26, 0.30],
        "risk.stop_atr": [4.0, 6.0, 8.0],
        "strategy.rebalance_days": [7, 14],
    }
    grid: list[dict] = []
    keys = list(grid_spec)
    er = engine
    for values in itertools.product(*[grid_spec[k] for k in keys]):
        override = {}
        for key, value in zip(keys, values):
            node = override
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        # stop_atr et trail_atr doivent bouger ensemble.
        if "risk" in override and "stop_atr" in override["risk"]:
            override["risk"]["trail_atr"] = override["risk"]["stop_atr"]
        candidate = deep_merge(cfg, override)
        er.cfg = candidate
        m_is = er.run(start=is_start, end=split_date).metrics
        m_oos = er.run(
            start=str((pd.Timestamp(split_date) + pd.Timedelta(days=1)).date())
        ).metrics
        grid.append(
            {
                "params": {k: v for k, v in zip(keys, values)},
                "is": _metrics_brief(m_is),
                "oos": _metrics_brief(m_oos),
                "score": _score(m_is),
            }
        )
    grid.sort(key=lambda row: row["score"], reverse=True)
    best = grid[0]
    frozen = bool(get(cfg, "research.freeze_mandate", False))
    if frozen:
        # Le mandat a été figé avant cette exécution. La grille reste un test
        # de sensibilité : elle ne doit pas réécrire le mandat publié, sinon
        # chaque run de recherche devient une nouvelle sélection.
        selected_cfg = cfg
        selected_params = {
            "strategy.target_vol": cfg["strategy"]["target_vol"],
            "strategy.rebalance_days": cfg["strategy"]["rebalance_days"],
            "risk.stop_atr": cfg["risk"]["stop_atr"],
            "risk.trail_atr": cfg["risk"]["trail_atr"],
        }
    else:
        selected_cfg = deep_merge(
            cfg,
            {
                "strategy": {
                    "target_vol": best["params"]["strategy.target_vol"],
                    "rebalance_days": best["params"]["strategy.rebalance_days"],
                },
                "risk": {
                    "stop_atr": best["params"]["risk.stop_atr"],
                    "trail_atr": best["params"]["risk.stop_atr"],
                },
            },
        )
        selected_params = best["params"]
    er.cfg = selected_cfg
    selected_full = er.run()
    selected_is = er.run(start=is_start, end=split_date)
    oos_start = str((pd.Timestamp(split_date) + pd.Timedelta(days=1)).date())
    selected_oos = er.run(start=oos_start)

    # Deux lectures de l'OOS, volontairement distinctes :
    # - nouveau compte : le capital est frais au début de l'OOS, il faut
    #   reconstruire les positions ;
    # - continu : les positions traversent la coupure, les rendements sont
    #   enchaînés et le drawdown se mesure depuis le plus haut historique.
    # Publier les deux évite de choisir celle qui arrange le récit.
    selected_oos_cont = window_metrics(
        selected_full.equity,
        oos_start,
        fresh=False,
        initial=float(cfg.get("initial_equity", 100_000.0)),
    )
    selected_is_cont = window_metrics(
        selected_full.equity,
        is_start,
        split_date,
        fresh=False,
        initial=float(cfg.get("initial_equity", 100_000.0)),
    )

    # Walk-forward : sélection sur le train, application au test, sans
    # re-optimiser pendant le test. Huit fenêtres semestrielles donnent plus
    # de points de contrôle que quatre fenêtres annuelles.
    wf_spec = {
        "risk.stop_atr": [4.0, 6.0, 8.0],
        "strategy.rebalance_days": [7, 14],
        "strategy.entry_score": [0.20, 0.25],
    }
    wf_keys = list(wf_spec)
    wf_configs = []
    for values in itertools.product(*[wf_spec[k] for k in wf_keys]):
        override = {}
        for key, value in zip(wf_keys, values):
            node = override
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        if "risk" in override and "stop_atr" in override["risk"]:
            override["risk"]["trail_atr"] = override["risk"]["stop_atr"]
        wf_configs.append(deep_merge(selected_cfg, override))

    wf_windows = _fold_ranges(
        store.dates[store.warmup_index()], store.dates[-1], folds=8, test_days=182
    )
    folds = []
    oos_returns = []
    for fold_start, fold_end in wf_windows:
        best_fold = None
        for candidate in wf_configs:
            er.cfg = candidate
            train = er.run(end=str((pd.Timestamp(fold_start) - pd.Timedelta(days=1)).date()))
            m = train.metrics
            score = _score(m)
            if best_fold is None or score > best_fold[0]:
                best_fold = (score, candidate, m)
        assert best_fold is not None
        er.cfg = best_fold[1]
        test = er.run(start=fold_start, end=fold_end)
        folds.append(
            {
                "test_start": fold_start,
                "test_end": fold_end,
                "selected": {
                    "stop_atr": best_fold[1]["risk"]["stop_atr"],
                    "rebalance_days": best_fold[1]["strategy"]["rebalance_days"],
                    "entry_score": best_fold[1]["strategy"]["entry_score"],
                },
                "train": _metrics_brief(best_fold[2]),
                "test": _metrics_brief(test.metrics),
            }
        )
        oos_returns.append(test.equity.pct_change().dropna())

    stitched = pd.concat(oos_returns) if oos_returns else pd.Series(dtype=float)
    stitched = stitched[~stitched.index.duplicated(keep="last")].sort_index()
    stitched_equity = (1.0 + stitched).cumprod() * float(cfg.get("initial_equity", 100_000.0))
    stitched_metrics = performance(stitched_equity)

    # Walk-forward à mandat fixe : aucune ré-optimisation. C'est le contrôle
    # le plus honnête du mandat réellement publié, et le seul qui ne peut pas
    # être accusé de sélection adaptative sur chaque fold.
    fixed_folds = []
    fixed_returns = []
    er.cfg = selected_cfg
    for fold_start, fold_end in wf_windows:
        test = er.run(start=fold_start, end=fold_end)
        fixed_folds.append(
            {
                "test_start": fold_start,
                "test_end": fold_end,
                "test": _metrics_brief(test.metrics),
            }
        )
        fixed_returns.append(test.equity.pct_change().dropna())
    fixed_stitched = pd.concat(fixed_returns) if fixed_returns else pd.Series(dtype=float)
    fixed_stitched = fixed_stitched[~fixed_stitched.index.duplicated(keep="last")].sort_index()
    fixed_equity = (1.0 + fixed_stitched).cumprod() * float(
        cfg.get("initial_equity", 100_000.0)
    )
    fixed_metrics = performance(fixed_equity)

    walkforward = {
        "folds": folds,
        "stitched": _metrics_brief(stitched_metrics),
        "positive_folds": int(sum(1 for f in folds if (f["test"].get("cagr") or 0) > 0)),
        "total_folds": len(folds),
        "test_days": 182,
        "fixed": {
            "folds": fixed_folds,
            "stitched": _metrics_brief(fixed_metrics),
            "positive_folds": int(
                sum(1 for f in fixed_folds if (f["test"].get("cagr") or 0) > 0)
            ),
            "total_folds": len(fixed_folds),
        },
    }

    # Monte Carlo sur la courbe sélectionnée.
    mc = monte_carlo(selected_full.equity, paths=paths, block=20)
    mc.pop("_cagrs", None)
    mc.pop("_dds", None)

    # Stress de coûts.
    cost_stress = []
    for mult in (1.0, 2.0, 3.0, 5.0):
        er.cfg = selected_cfg
        m = er.run(cost_multiplier=mult).metrics
        cost_stress.append({"multiplier": mult, **_metrics_brief(m)})

    # Robustesse au point de départ (portefeuille frais à chaque date).
    start_dates = []
    for year in range(2020, 2027):
        er.cfg = selected_cfg
        start = max(store.dates[store.warmup_index()], pd.Timestamp(f"{year}-01-01", tz="UTC"))
        if start >= store.dates[-1]:
            continue
        m = er.run(start=str(start.date())).metrics
        start_dates.append({"start": str(start.date()), **_metrics_brief(m)})

    # Sensibilité univariée (paramètres de risque, pas de signal).
    sensitivity: dict[str, list[dict]] = {}
    for param, values in {
        "strategy.target_vol": [0.20, 0.25, 0.30, 0.35],
        "risk.stop_atr": [4.0, 5.0, 6.0, 7.0, 8.0],
        "strategy.rebalance_days": [5, 7, 10, 14],
        "strategy.entry_score": [0.15, 0.20, 0.25, 0.30],
    }.items():
        rows = []
        for value in values:
            override = {}
            node = override
            parts = param.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            if param == "risk.stop_atr":
                override["risk"]["trail_atr"] = value
            er.cfg = deep_merge(selected_cfg, override)
            rows.append({"value": value, **_metrics_brief(er.run().metrics)})
        sensitivity[param] = rows

    report = ResearchReport(
        started_at=pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        elapsed_sec=round(time.time() - t0, 1),
        config={
            "universe": cfg["universe"],
            "target_vol": selected_cfg["strategy"]["target_vol"],
            "stop_atr": selected_cfg["risk"]["stop_atr"],
            "rebalance_days": selected_cfg["strategy"]["rebalance_days"],
            "entry_score": selected_cfg["strategy"]["entry_score"],
            "costs": cfg["costs"],
        },
        split={
            "is_start": is_start,
            "is_end": split_date,
            "oos_start": str((pd.Timestamp(split_date) + pd.Timedelta(days=1)).date()),
            "oos_end": str(store.dates[-1].date()),
            "ratio": 0.70,
        },
        grid=grid,
        selected={
            "mode": "frozen" if frozen else "is_grid",
            "params": selected_params,
            "grid_best": best["params"],
            "is": _metrics_brief(selected_is.metrics),
            "is_continuous": _metrics_brief(selected_is_cont),
            "oos": _metrics_brief(selected_oos.metrics),
            "oos_continuous": _metrics_brief(selected_oos_cont),
            "full": _metrics_brief(selected_full.metrics),
        },
        oos={
            "is": _metrics_brief(is_res.metrics),
            "oos": _metrics_brief(oos_res.metrics),
            "oos_continuous": _metrics_brief(selected_oos_cont),
            "full": _metrics_brief(full.metrics),
        },
        walkforward=walkforward,
        montecarlo=mc,
        cost_stress=cost_stress,
        start_dates=start_dates,
        sensitivity=sensitivity,
        notes=[
            "Sélection sur l'IS uniquement (Sharpe, contrainte DD > -22%, turnover < 9x).",
            *(
                [
                    "Mandat figé avant cette exécution : la grille est une sensibilité, pas une sélection.",
                ]
                if frozen
                else []
            ),
            "L'OOS n'a pas servi à sélectionner : il est publié après coup, y compris s'il déçoit.",
            "Deux OOS sont publiés : nouveau compte (capital frais) et continu (positions traversantes).",
            "Le walk-forward optimisé ré-optimise sur chaque train et applique au test suivant.",
            "Le walk-forward à mandat fixe applique le mandat publié sans ré-optimisation.",
            "Le Monte Carlo est un bootstrap par blocs de 20 jours ; ce n'est pas une prévision.",
            "Les coûts incluent frais taker, slippage et impact ; le stress multiplie les trois.",
            "Aucune garantie : un backtest reste une simulation avec ses hypothèses.",
        ],
    )
    os.makedirs(RUNS, exist_ok=True)
    with open(os.path.join(RUNS, "research.json"), "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2, ensure_ascii=False)
    pd.DataFrame(
        [
            {
                "stop_atr": row["params"]["risk.stop_atr"],
                "rebalance_days": row["params"]["strategy.rebalance_days"],
                "target_vol": row["params"]["strategy.target_vol"],
                "is_sharpe": row["is"]["sharpe"],
                "is_cagr": row["is"]["cagr"],
                "is_dd": row["is"]["max_drawdown"],
                "oos_sharpe": row["oos"]["sharpe"],
                "oos_cagr": row["oos"]["cagr"],
                "oos_dd": row["oos"]["max_drawdown"],
                "score": row["score"],
            }
            for row in grid
        ]
    ).to_csv(os.path.join(RUNS, "research_grid.csv"), index=False)
    return report


def summarize(report: ResearchReport) -> str:
    lines = []
    sel = report.selected
    lines.append("=" * 78)
    lines.append("MERIDIAN — RÉSULTATS DE RECHERCHE")
    lines.append("=" * 78)
    lines.append(f"Période IS  : {report.split['is_start']} → {report.split['is_end']}")
    lines.append(f"Période OOS : {report.split['oos_start']} → {report.split['oos_end']}")
    lines.append(
        f"Mandat      : {'figé' if sel.get('mode') == 'frozen' else 'sélectionné sur IS'} · "
        f"target_vol {sel['params']['strategy.target_vol']:.0%}, "
        f"stop {sel['params']['risk.stop_atr']:.0f} ATR, "
        f"rebalance {sel['params']['strategy.rebalance_days']}j"
    )
    for label, key in (
        ("IS", "is"),
        ("OOS neuf", "oos"),
        ("OOS cont.", "oos_continuous"),
        ("FULL", "full"),
    ):
        m = sel[key]
        turnover = m.get("turnover_x")
        turnover_text = (
            f"{turnover:.1f}x"
            if isinstance(turnover, (int, float)) and np.isfinite(turnover)
            else "n/a"
        )
        lines.append(
            f"  {label:9s}: CAGR {m['cagr']*100:6.1f}% · vol {m['volatility']*100:5.1f}% · "
            f"Sharpe {m['sharpe']:.2f} · DD {m['max_drawdown']*100:6.1f}% · "
            f"turnover {turnover_text}"
        )
    wf = report.walkforward
    lines.append(
        f"Walk-forward optimisé : {wf['positive_folds']}/{wf['total_folds']} folds positifs · "
        f"CAGR {wf['stitched']['cagr']*100:.1f}% · Sharpe {wf['stitched']['sharpe']:.2f} · "
        f"DD {wf['stitched']['max_drawdown']*100:.1f}%"
    )
    fixed = wf.get("fixed", {})
    if fixed:
        lines.append(
            f"Walk-forward fixe   : {fixed['positive_folds']}/{fixed['total_folds']} folds positifs · "
            f"CAGR {fixed['stitched']['cagr']*100:.1f}% · Sharpe {fixed['stitched']['sharpe']:.2f} · "
            f"DD {fixed['stitched']['max_drawdown']*100:.1f}%"
        )
    mc = report.montecarlo
    lines.append(
        f"Monte Carlo  : CAGR p5/p50/p95 {mc['cagr_p5']*100:.1f}% / {mc['cagr_p50']*100:.1f}% / "
        f"{mc['cagr_p95']*100:.1f}% · DD p5 {mc['maxdd_p5']*100:.1f}% · "
        f"P(perte) {mc['prob_negative']*100:.0f}%"
    )
    lines.append("Stress coûts :")
    for row in report.cost_stress:
        lines.append(
            f"  ×{row['multiplier']:.0f} : CAGR {row['cagr']*100:5.1f}% · Sharpe {row['sharpe']:.2f} · "
            f"DD {row['max_drawdown']*100:5.1f}%"
        )
    lines.append("Départs annuels :")
    for row in report.start_dates:
        lines.append(
            f"  {row['start']} : CAGR {row['cagr']*100:5.1f}% · Sharpe {row['sharpe']:.2f} · "
            f"DD {row['max_drawdown']*100:5.1f}%"
        )
    lines.append(f"Écrit dans {os.path.join(RUNS, 'research.json')} ({report.elapsed_sec:.0f}s)")
    return "\n".join(lines)
