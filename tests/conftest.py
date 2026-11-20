"""Fixtures partagées : un panel synthétique court pour des tests rapides.

Les données synthétiques ne servent qu'aux tests. Le paper et le backtest
réels utilisent les CSV Binance du dossier ``data/``.

La date de fin est figée : ``synthetic_panel`` se termine sinon au jour
courant, ce qui décale la fenêtre de test chaque jour et rend certains
résultats path-dependent instables (par exemple le test de stress de coûts).
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meridian.config import load_config
from meridian.data import synthetic_panel

# Fin fixe du panel de test, indépendante de la date d'exécution.
SYNTHETIC_END = pd.Timestamp("2026-06-30", tz="UTC")
SYNTHETIC_DAYS = 1400


@pytest.fixture(scope="session")
def cfg() -> dict:
    return load_config()


@pytest.fixture(scope="session")
def panel(cfg) -> dict[str, pd.DataFrame]:
    data = synthetic_panel(cfg, seed=13)
    data = {
        symbol: frame.loc[frame.index <= SYNTHETIC_END].copy()
        for symbol, frame in data.items()
    }
    cutoff = SYNTHETIC_END - pd.Timedelta(days=SYNTHETIC_DAYS)
    return {
        symbol: frame.loc[frame.index >= cutoff].copy()
        for symbol, frame in data.items()
    }


@pytest.fixture(scope="session")
def store(cfg, panel):
    from meridian.features import FeatureStore

    return FeatureStore.build(panel, cfg)


@pytest.fixture()
def engine(cfg, store):
    from meridian.backtest import Backtest

    return Backtest(store, cfg)
