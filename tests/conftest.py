"""Fixtures partagées : un panel synthétique court pour des tests rapides.

Les données synthétiques ne servent qu'aux tests. Le paper et le backtest
réels utilisent les CSV Binance du dossier ``data/``.
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meridian.config import load_config
from meridian.data import synthetic_panel


@pytest.fixture(scope="session")
def cfg() -> dict:
    return load_config()


@pytest.fixture(scope="session")
def panel(cfg) -> dict[str, pd.DataFrame]:
    data = synthetic_panel(cfg, seed=13)
    last = max(df.index[-1] for df in data.values())
    cutoff = last - pd.Timedelta(days=1400)
    return {symbol: df.loc[df.index >= cutoff].copy() for symbol, df in data.items()}


@pytest.fixture(scope="session")
def store(cfg, panel):
    from meridian.features import FeatureStore

    return FeatureStore.build(panel, cfg)


@pytest.fixture()
def engine(cfg, store):
    from meridian.backtest import Backtest

    return Backtest(store, cfg)
