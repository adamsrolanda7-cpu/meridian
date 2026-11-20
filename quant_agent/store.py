"""Cache de backtests reproductibles.

Chaque run est identifié par un hash stable de :

- la stratégie et sa configuration effective ;
- la fenêtre et le capital initial ;
- un snapshot des données utilisées (nombre de bougies et dernière date).

Le même calcul sur les mêmes données renvoie le même ``run_id`` et le cache
est réutilisé sans consommer de quota de backtest.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

from meridian.config import ROOT

from .jsonutil import stable_hash, to_jsonable

RUN_PREFIX = "bt_"
RUN_ID_LENGTH = 12


def compute_run_id(
    *,
    strategy: str,
    cfg: dict[str, Any],
    start: str,
    end: str,
    initial_equity: float,
    cost_multiplier: float,
    data_snapshot: dict[str, Any],
) -> str:
    """Hash stable d'un backtest."""
    payload = {
        "engine": "meridian",
        "engine_version": 1,
        "strategy": strategy,
        "config": cfg,
        "start": start,
        "end": end,
        "initial_equity": float(initial_equity),
        "cost_multiplier": float(cost_multiplier),
        "data": data_snapshot,
    }
    return RUN_PREFIX + stable_hash(payload, RUN_ID_LENGTH)


def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


class RunStore:
    """Stockage JSON local des backtests."""

    def __init__(self, root: str | None = None):
        self.root = root or ROOT
        self.base_dir = os.path.join(self.root, "runs", "agent")
        self.runs_dir = os.path.join(self.base_dir, "backtests")
        self.index_path = os.path.join(self.base_dir, "index.json")

    # ------------------------------------------------------------------ paths
    def run_path(self, run_id: str) -> str:
        return os.path.join(self.runs_dir, f"{run_id}.json")

    def exists(self, run_id: str) -> bool:
        return os.path.exists(self.run_path(run_id))

    # ------------------------------------------------------------------- load
    def load(self, run_id: str) -> dict[str, Any] | None:
        if not run_id or not run_id.startswith(RUN_PREFIX):
            return None
        path = self.run_path(run_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("run_id") != run_id:
            return None
        return payload

    def latest(self) -> dict[str, Any] | None:
        index = self._read_index()
        for entry in index:
            payload = self.load(entry.get("run_id", ""))
            if payload is not None:
                return payload
        # Fallback : le fichier le plus récent.
        if not os.path.isdir(self.runs_dir):
            return None
        files = [
            os.path.join(self.runs_dir, name)
            for name in os.listdir(self.runs_dir)
            if name.startswith(RUN_PREFIX) and name.endswith(".json")
        ]
        if not files:
            return None
        files.sort(key=os.path.getmtime, reverse=True)
        run_id = os.path.basename(files[0])[: -len(".json")]
        return self.load(run_id)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._read_index()[: max(0, int(limit))]

    # ------------------------------------------------------------------- save
    def save(self, payload: dict[str, Any]) -> str:
        run_id = str(payload["run_id"])
        body = to_jsonable(payload)
        _atomic_write(
            self.run_path(run_id),
            json.dumps(body, indent=2, ensure_ascii=False) + "\n",
        )
        self._update_index(body)
        return run_id

    def _read_index(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.index_path):
            return []
        try:
            with open(self.index_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
        runs = data.get("runs") if isinstance(data, dict) else None
        return runs if isinstance(runs, list) else []

    def _update_index(self, payload: dict[str, Any]) -> None:
        index = [
            entry
            for entry in self._read_index()
            if entry.get("run_id") != payload.get("run_id")
        ]
        index.insert(0, summary_from_payload(payload))
        index = index[:100]
        _atomic_write(
            self.index_path,
            json.dumps({"version": 1, "runs": index}, indent=2, ensure_ascii=False)
            + "\n",
        )


SUMMARY_METRICS = (
    "start",
    "end",
    "days",
    "initial_equity",
    "final_equity",
    "total_return",
    "cagr",
    "volatility",
    "sharpe",
    "sortino",
    "calmar",
    "max_drawdown",
    "turnover_x",
    "fills",
    "hit_rate",
    "profit_factor",
    "trading_costs",
    "avg_gross_exposure",
)


def summary_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Vue compacte d'un run, adaptée au cache et à l'agent."""
    metrics = payload.get("metrics") or {}
    return {
        "run_id": payload.get("run_id"),
        "created_at": payload.get("created_at"),
        "strategy": payload.get("strategy"),
        "label": payload.get("label"),
        "start": payload.get("start"),
        "end": payload.get("end"),
        "initial_equity": payload.get("initial_equity"),
        "data_last_bar": (payload.get("data") or {}).get("last_bar"),
        "metrics": {key: metrics.get(key) for key in SUMMARY_METRICS},
        "warnings": payload.get("warnings", []),
    }


def compact_run(payload: dict[str, Any], include_curve: bool = False) -> dict[str, Any]:
    """Réponse de l'outil ``get_backtest``."""
    out = summary_from_payload(payload)
    out["metrics"] = to_jsonable(payload.get("metrics") or {})
    out["monthly_returns"] = payload.get("monthly_returns", [])
    out["recent_trades"] = payload.get("recent_trades", [])
    out["diagnostics"] = payload.get("diagnostics", {})
    out["config_hash"] = payload.get("config_hash")
    if include_curve:
        out["equity_curve"] = payload.get("equity_curve", [])
    return out


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compare_payloads(
    a: dict[str, Any], b: dict[str, Any]
) -> dict[str, Any]:
    """Compare deux runs et calcule des deltas B - A."""
    metrics_a = a.get("metrics") or {}
    metrics_b = b.get("metrics") or {}
    keys = (
        "cagr",
        "volatility",
        "sharpe",
        "sortino",
        "calmar",
        "max_drawdown",
        "turnover_x",
        "hit_rate",
        "profit_factor",
        "fills",
    )
    deltas: dict[str, float | None] = {}
    for key in keys:
        left = _finite(metrics_a.get(key))
        right = _finite(metrics_b.get(key))
        deltas[key] = (right - left) if left is not None and right is not None else None

    correlation = None
    curve_a = a.get("equity_curve") or []
    curve_b = b.get("equity_curve") or []
    if curve_a and curve_b:
        correlation = _curve_correlation(curve_a, curve_b)

    return {
        "runs": [
            {
                "run_id": a.get("run_id"),
                "strategy": a.get("strategy"),
                "label": a.get("label"),
                "metrics": {key: metrics_a.get(key) for key in keys},
            },
            {
                "run_id": b.get("run_id"),
                "strategy": b.get("strategy"),
                "label": b.get("label"),
                "metrics": {key: metrics_b.get(key) for key in keys},
            },
        ],
        "deltas": deltas,
        "correlation": correlation,
        "note": (
            "Les performances passées ne préjugent pas des performances futures. "
            "Une différence de Sharpe ou de CAGR n'est pas une recommandation."
        ),
    }


def _curve_correlation(
    curve_a: list[dict[str, Any]], curve_b: list[dict[str, Any]]
) -> float | None:
    """Corrélation des rendements quotidiens, sans dépendance à pandas."""
    map_a = {
        str(point.get("date"))[:10]: _finite(point.get("equity")) for point in curve_a
    }
    map_b = {
        str(point.get("date"))[:10]: _finite(point.get("equity")) for point in curve_b
    }
    dates = sorted(set(map_a) & set(map_b))
    if len(dates) < 10:
        return None
    values_a = [map_a[day] for day in dates]
    values_b = [map_b[day] for day in dates]
    rets_a: list[float] = []
    rets_b: list[float] = []
    for i in range(1, len(dates)):
        if not values_a[i - 1] or not values_b[i - 1]:
            continue
        rets_a.append(values_a[i] / values_a[i - 1] - 1.0)
        rets_b.append(values_b[i] / values_b[i - 1] - 1.0)
    if len(rets_a) < 10:
        return None
    mean_a = sum(rets_a) / len(rets_a)
    mean_b = sum(rets_b) / len(rets_b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(rets_a, rets_b))
    var_a = sum((x - mean_a) ** 2 for x in rets_a)
    var_b = sum((y - mean_b) ** 2 for y in rets_b)
    if var_a <= 0 or var_b <= 0:
        return None
    return cov / math.sqrt(var_a * var_b)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
