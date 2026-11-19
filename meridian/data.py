"""Données de marché : téléchargement Binance, cache disque, contrôle qualité.

Source primaire : API publique Binance (aucune clé requise). En cas d'échec
réseau, le cache local est utilisé ; en dernier recours seulement, un jeu de
données synthétique déterministe permet de faire tourner les tests et la démo.
Le statut de la source est toujours exposé pour éviter de présenter du
synthétique comme du réel.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable

import numpy as np
import pandas as pd

from .config import ROOT, get

BAR_MS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000}
DATA_DIR = os.path.join(ROOT, "data")
META_PATH = os.path.join(DATA_DIR, "meta.json")
QUALITY_PATH = os.path.join(DATA_DIR, "quality.json")

_HEADERS = {"User-Agent": "meridian-desk/1.0", "Accept": "application/json"}


class DataError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_json(url: str, params: dict, timeout: int = 20, attempts: int = 3):
    query = urllib.parse.urlencode(params)
    full = f"{url}?{query}"
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(full, headers=_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (418, 429) and attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            if 400 <= exc.code < 500 and exc.code not in (408, 425):
                break
        except Exception as exc:  # réseau, timeout, JSON
            last_error = exc
        if attempt < attempts - 1:
            time.sleep(0.5 * (attempt + 1))
    raise DataError(f"échec HTTP {url}: {last_error}")


def fetch_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int | None = None,
    base_url: str = "https://data-api.binance.vision",
    fallback_url: str | None = "https://api.binance.com",
) -> list[list]:
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": int(start_ms),
        "limit": 1000,
    }
    if end_ms:
        params["endTime"] = int(end_ms)
    try:
        return _get_json(f"{base_url}/api/v3/klines", params)
    except Exception as primary:
        if fallback_url:
            try:
                return _get_json(f"{fallback_url}/api/v3/klines", params)
            except Exception:
                pass
        raise DataError(f"Binance indisponible pour {symbol}: {primary}") from primary


def download_symbol(
    symbol: str,
    interval: str,
    start: str,
    base_url: str,
    fallback_url: str | None,
    quiet: bool = False,
) -> pd.DataFrame:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    step = BAR_MS.get(interval, 86_400_000)
    cursor = start_ms
    rows: list[list] = []
    now = _now_ms()

    while cursor < now:
        batch = fetch_klines(symbol, interval, cursor, base_url=base_url, fallback_url=fallback_url)
        if not batch:
            break
        # On ne conserve que les bougies clôturées : la dernière bougie en
        # formation ne doit jamais alimenter une décision.
        closed = [k for k in batch if int(k[6]) < now]
        rows.extend(closed)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1][0]) + step
        if not quiet:
            print(f"  {symbol}: {len(rows)} bougies…", end="\r", flush=True)

    if not rows:
        raise DataError(f"aucune bougie reçue pour {symbol}")

    df = pd.DataFrame(
        rows,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_base",
            "taker_quote",
            "ignore",
        ],
    )
    df = df.drop_duplicates(subset="open_time").sort_values("open_time")
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trades"] = pd.to_numeric(df["trades"], errors="coerce").fillna(0).astype("int64")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[df["close"] > 0]
    return df.set_index("date")[["open", "high", "low", "close", "volume", "quote_volume", "trades"]]


def download_all(
    cfg: dict,
    symbols: Iterable[str] | None = None,
    start: str | None = None,
    force: bool = False,
    quiet: bool = False,
) -> dict[str, pd.DataFrame]:
    os.makedirs(DATA_DIR, exist_ok=True)
    symbols = list(symbols or cfg["universe"])
    start = start or cfg.get("start", "2019-01-01")
    interval = cfg.get("interval", "1d")
    base_url = get(cfg, "data.base_url", "https://data-api.binance.vision")
    fallback = get(cfg, "data.fallback_url", "https://api.binance.com")
    meta = _read_json(META_PATH, {})
    panel: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}

    for symbol in symbols:
        path = os.path.join(DATA_DIR, f"{symbol}_{interval}.csv")
        expected_last = meta.get(symbol, {}).get("last", "")[:10]
        fresh = (
            os.path.exists(path)
            and expected_last >= (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        )
        if fresh and not force:
            try:
                panel[symbol] = _read_csv(path)
                if not quiet:
                    print(f"  {symbol}: cache local ({len(panel[symbol])} lignes)")
                continue
            except Exception:
                pass
        if not quiet:
            print(f"  {symbol}: téléchargement depuis {start}…")
        try:
            df = download_symbol(symbol, interval, start, base_url, fallback, quiet=quiet)
            df.to_csv(path, index_label="date")
            panel[symbol] = df
            meta[symbol] = {
                "symbol": symbol,
                "interval": interval,
                "source": "binance",
                "rows": int(len(df)),
                "first": str(df.index[0]),
                "last": str(df.index[-1]),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if not quiet:
                print(f"  {symbol}: {len(df)} bougies, {df.index[0].date()} → {df.index[-1].date()}")
        except Exception as exc:
            failures[symbol] = str(exc)
            if os.path.exists(path):
                panel[symbol] = _read_csv(path)
                if not quiet:
                    print(f"  {symbol}: réseau KO, cache utilisé ({exc})")
            elif not quiet:
                print(f"  {symbol}: échec ({exc})")

    if meta:
        _write_json(META_PATH, meta)
    if failures:
        _write_json(os.path.join(DATA_DIR, "download_errors.json"), failures)
    if not panel:
        raise DataError("aucune donnée disponible (réseau et cache absents)")
    return panel


def _read_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"], index_col="date")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df.sort_index()
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_panel(cfg: dict, symbols: Iterable[str] | None = None) -> dict[str, pd.DataFrame]:
    symbols = list(symbols or cfg["universe"])
    interval = cfg.get("interval", "1d")
    panel: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = os.path.join(DATA_DIR, f"{symbol}_{interval}.csv")
        if not os.path.exists(path):
            raise DataError(f"{path} absent : lancez `python -m meridian download`")
        panel[symbol] = _read_csv(path)
    return panel


def synthetic_panel(cfg: dict, symbols: Iterable[str] | None = None, seed: int = 7) -> dict[str, pd.DataFrame]:
    """Sentier synthétique à régimes, corrélé, pour tests hors ligne uniquement."""
    symbols = list(symbols or cfg["universe"])
    start = pd.Timestamp(cfg.get("start", "2019-01-01"), tz="UTC")
    end = pd.Timestamp.now(tz="UTC").normalize()
    dates = pd.date_range(start, end, freq="D")
    n = len(dates)
    rng = np.random.default_rng(seed)

    # Régimes : 0 = bull, 1 = chop, 2 = bear, avec persistance.
    transition = np.array([[0.985, 0.012, 0.003], [0.010, 0.975, 0.015], [0.008, 0.022, 0.970]])
    states = np.zeros(n, dtype=int)
    for i in range(1, n):
        states[i] = rng.choice(3, p=transition[states[i - 1]])
    drift = np.array([0.0011, 0.0000, -0.0013])
    common_vol = 0.030 + 0.020 * (states == 2)
    common = rng.normal(drift[states], common_vol)

    params = {
        "BTCUSDT": (1.00, 0.022, 3800.0),
        "ETHUSDT": (1.15, 0.032, 130.0),
        "SOLUSDT": (1.45, 0.052, 2.8),
        "LTCUSDT": (0.90, 0.028, 30.0),
    }
    panel: dict[str, pd.DataFrame] = {}
    listed = {"BTCUSDT": 0, "ETHUSDT": 0, "SOLUSDT": 580, "LTCUSDT": 0}
    for symbol in symbols:
        beta, idio_vol, price0 = params.get(symbol, (1.0, 0.03, 100.0))
        shocks = beta * common + rng.normal(0.0, idio_vol, n)
        # Volatilité qui persiste (cluster) pour rendre les stops réalistes.
        vol_mult = np.ones(n)
        for i in range(1, n):
            vol_mult[i] = 0.94 * vol_mult[i - 1] + 0.06 * (1.0 + 0.8 * abs(shocks[i - 1]) / 0.03)
        rets = shocks * np.clip(vol_mult, 0.5, 3.0)
        prices = price0 * np.exp(np.cumsum(rets))
        prices[: listed.get(symbol, 0)] = np.nan
        valid = ~np.isnan(prices)
        close = pd.Series(prices, index=dates)
        prev = close.shift(1)
        open_ = prev.fillna(close) * (1.0 + rng.normal(0, 0.0015, n))
        high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.005, n)))
        low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.005, n)))
        volume = rng.lognormal(mean=10.0, sigma=0.6, size=n)
        quote = volume * close
        df = pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "quote_volume": quote,
                "trades": rng.integers(5_000, 90_000, n),
            },
            index=dates,
        )
        df = df[valid].dropna()
        for col in ("open", "high", "low"):
            df[col] = df[col].clip(lower=1e-6)
        panel[symbol] = df
    return panel


def ensure_data(cfg: dict, quiet: bool = False, allow_synthetic: bool = True) -> tuple[dict[str, pd.DataFrame], str]:
    """Charge les données, télécharge si nécessaire, sinon synthétique."""
    try:
        panel = load_panel(cfg)
        meta = _read_json(META_PATH, {})
        last = max(df.index[-1] for df in panel.values())
        stale_days = (pd.Timestamp.now(tz="UTC") - last).days
        if stale_days > 5:
            if not quiet:
                print(f"  cache vieux de {stale_days} jours, tentative de mise à jour…")
            panel = download_all(cfg, quiet=quiet)
        source = "binance/cache" if all(meta.get(s, {}).get("source") == "binance" for s in panel) else "cache"
        return panel, source
    except Exception as exc:
        if not quiet:
            print(f"  données locales indisponibles ({exc}), téléchargement…")
    try:
        panel = download_all(cfg, quiet=quiet)
        return panel, "binance"
    except Exception as exc:
        if not allow_synthetic:
            raise DataError(str(exc)) from exc
        if not quiet:
            print(f"  ATTENTION: source réelle indisponible ({exc}). Données SYNTHÉTIQUES utilisées.")
        return synthetic_panel(cfg), "synthetic"


def quality_report(cfg: dict, panel: dict[str, pd.DataFrame]) -> dict:
    interval = cfg.get("interval", "1d")
    step_days = {"1d": 1, "4h": 0.1667, "1h": 0.0417}.get(interval, 1)
    report: dict[str, dict] = {}
    for symbol, df in panel.items():
        if df.empty:
            report[symbol] = {"status": "EMPTY", "rows": 0}
            continue
        diffs = df.index.to_series().diff().dt.total_seconds() / 86400.0
        gaps = int((diffs > step_days * 1.5).sum()) if len(df) > 1 else 0
        ohlc_bad = int(
            (
                (df["high"] < df[["open", "close"]].max(axis=1))
                | (df["low"] > df[["open", "close"]].min(axis=1))
                | (df["high"] < df["low"])
            ).sum()
        )
        stale = int((pd.Timestamp.now(tz="UTC") - df.index[-1]).days)
        dupes = int(df.index.duplicated().sum())
        status = "OK"
        if ohlc_bad or dupes or df.isna().any().any():
            status = "WARN"
        if gaps > 2 or stale > 10:
            status = "WARN"
        report[symbol] = {
            "status": status,
            "rows": int(len(df)),
            "first": str(df.index[0].date()),
            "last": str(df.index[-1].date()),
            "stale_days": stale,
            "gaps": gaps,
            "duplicates": dupes,
            "ohlc_violations": ohlc_bad,
            "zero_volume_bars": int((df["volume"] <= 0).sum()),
            "last_close": float(df["close"].iloc[-1]),
            "adv20_usd": float((df["quote_volume"].tail(20)).mean()),
        }
    _write_json(QUALITY_PATH, report)
    return report
