"""Chargement et validation de la configuration du desk.

La configuration est un dictionnaire JSON validé, avec fusion profonde pour
les surcharges (recherche de paramètres, tests, paper trading). Aucun secret
n'est stocké ici : les clés d'exchange, si un jour elles sont utilisées,
doivent venir de l'environnement.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "config", "default.json")


def deep_merge(base: dict, override: dict | None) -> dict:
    """Retourne une copie de ``base`` avec ``override`` fusionnée récursivement."""
    out = copy.deepcopy(base)
    if not override:
        return out
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(path: str | None = None, overrides: dict | None = None) -> dict:
    path = path or DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg = deep_merge(cfg, overrides)
    errors = validate_config(cfg)
    if errors:
        raise ValueError("configuration invalide: " + "; ".join(errors))
    return cfg


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    """Accès par clé pointée, ex. ``get(cfg, "strategy.target_vol")``."""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_in(cfg: dict, dotted: str, value: Any) -> dict:
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return cfg


def validate_config(cfg: dict) -> list[str]:
    """Contrôles de cohérence économique, pas seulement de type."""
    errors: list[str] = []

    def num(dotted: str, lo: float, hi: float) -> None:
        value = get(cfg, dotted)
        if not isinstance(value, (int, float)) or not (lo <= float(value) <= hi):
            errors.append(f"{dotted} doit être dans [{lo}, {hi}]")

    if not cfg.get("universe"):
        errors.append("universe vide")
    for symbol in cfg.get("universe", []):
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            errors.append(f"symbole non supporté: {symbol}")

    if get(cfg, "strategy.allow_short"):
        # Le moteur de comptabilité est long-only spot. On refuse plutôt que
        # de simuler un short avec des hypothèses de financement implicites.
        errors.append("allow_short=true non supporté (spot long-only)")

    num("strategy.target_vol", 0.02, 1.00)
    num("strategy.max_gross", 0.05, 1.00)
    num("strategy.min_cash", 0.00, 0.90)
    num("strategy.max_asset_weight", 0.05, 1.00)
    num("strategy.rebalance_threshold", 0.0, 0.25)
    num("strategy.cov_lambda", 0.50, 0.999)
    num("strategy.cov_shrink", 0.0, 1.0)
    num("strategy.cov_window", 20, 365)
    num("strategy.trend.ema_fast", 5, 200)
    num("strategy.trend.ema_slow", 20, 500)
    num("strategy.trend.mom_win", 10, 365)
    num("strategy.trend.breakout_win", 10, 300)
    num("strategy.trend.adx_min", 0, 60)
    num("strategy.meanrev.bb_win", 5, 100)
    num("strategy.meanrev.z_entry", 0.5, 4.0)
    num("strategy.meanrev.rsi_entry", 10, 50)
    num("strategy.meanrev.max_hold", 1, 60)
    num("strategy.xsmom.mom_win", 10, 365)
    num("strategy.xsmom.top_n", 1, 10)
    num("strategy.regime.adx_trend", 5, 50)
    num("strategy.regime.vol_crisis_pct", 0.5, 0.999)
    num("strategy.regime.dd_crisis", 0.02, 0.80)
    num("strategy.regime.confirm_bars", 0, 20)

    num("risk.atr_win", 2, 100)
    num("risk.stop_atr", 0.5, 12.0)
    num("risk.mr_stop_atr", 0.5, 12.0)
    num("risk.trail_atr", 0.0, 12.0)
    num("risk.cooldown_bars", 0, 60)
    num("risk.daily_loss_limit", 0.001, 0.25)
    num("risk.dd_derisk", 0.01, 0.50)
    num("risk.dd_halt", 0.02, 0.80)
    num("risk.halt_bars", 1, 365)
    num("risk.max_adv_pct", 0.001, 0.50)
    num("risk.position_adv_pct", 0.001, 0.20)
    num("risk.vol_floor", 0.01, 1.0)
    num("costs.fee_bps", 0.0, 100.0)
    num("costs.slippage_bps", 0.0, 200.0)
    num("costs.impact_coef_bps", 0.0, 200.0)

    if get(cfg, "risk.dd_derisk", 0) >= get(cfg, "risk.dd_halt", 1):
        errors.append("risk.dd_derisk doit être < risk.dd_halt")
    if get(cfg, "strategy.trend.ema_fast", 0) >= get(cfg, "strategy.trend.ema_slow", 1):
        errors.append("ema_fast doit être < ema_slow")
    if get(cfg, "strategy.target_vol", 0) > 0.5:
        errors.append("target_vol > 50% : mandat trop agressif")
    if get(cfg, "strategy.max_asset_weight", 0) * len(cfg.get("universe", [])) < get(
        cfg, "strategy.max_gross", 0
    ):
        errors.append("max_asset_weight incompatible avec max_gross (trop concentré)")
    if get(cfg, "strategy.min_cash", 0) + get(cfg, "strategy.max_gross", 0) > 1.0001:
        errors.append("min_cash + max_gross > 100%")
    return errors


def dump_config(cfg: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
