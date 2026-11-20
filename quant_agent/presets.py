"""Stratégies exposées au CLI.

Deux presets seulement pour le prototype :

- ``trend_momentum`` : configuration Meridian par défaut (trend + XS momentum) ;
- ``mean_reversion`` : sleeve mean-reversion activé, XS momentum coupé.

Le moteur reste celui de Meridian. Les presets ne sont que des surcharges de
configuration validées par ``meridian.config.load_config``.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from meridian.config import load_config

DEFAULT_STRATEGY = "trend_momentum"
DEFAULT_LOOKBACK_DAYS = 730

STRATEGIES: dict[str, dict[str, Any]] = {
    "trend_momentum": {
        "name": "trend_momentum",
        "label": "Trend + momentum transversal",
        "description": (
            "Suit la tendance moyen terme (TSMOM, empilement EMA, cassure "
            "Donchian) et renforce les deux actifs les plus forts. Sleeve "
            "mean-reversion désactivé, XS momentum activé."
        ),
        "default": True,
        "overrides": {},
    },
    "mean_reversion": {
        "name": "mean_reversion",
        "label": "Mean reversion + filtre de tendance",
        "description": (
            "Active les achats de survente en régime RANGE (z-score, RSI) avec "
            "sortie rapide et stop serré. Le sleeve trend reste le filtre "
            "directionnel ; le XS momentum est coupé."
        ),
        "default": False,
        "overrides": {
            "strategy": {
                "meanrev": {
                    "enabled": True,
                    "market_breadth_min": 0.5,
                },
                "xsmom": {
                    "enabled": False,
                },
            }
        },
    },
}


def get_strategy(name: str) -> dict[str, Any]:
    """Retourne un preset ou lève une erreur lisible."""
    key = (name or "").strip().lower()
    if key not in STRATEGIES:
        available = ", ".join(STRATEGIES)
        raise KeyError(f"stratégie inconnue « {name} » ; disponibles : {available}")
    return deepcopy(STRATEGIES[key])


def list_strategies() -> list[dict[str, Any]]:
    """Liste publique des presets, sans exposer les objets internes."""
    out = []
    for preset in STRATEGIES.values():
        item = deepcopy(preset)
        item["overrides"] = deepcopy(preset["overrides"])
        out.append(item)
    return out


def build_config(name: str) -> dict[str, Any]:
    """Construit une config Meridian validée pour un preset."""
    preset = get_strategy(name)
    return load_config(overrides=preset["overrides"])
