"""Dashboard terminal : snapshot, rendu texte et absence de NaN."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

import meridian.live as live
import meridian.paper as paper


@pytest.fixture()
def live_env(cfg, panel, tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "PAPER_DIR", str(tmp_path))
    monkeypatch.setattr(paper, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper, "JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setattr(paper, "MEMO_PATH", str(tmp_path / "memo.md"))
    monkeypatch.setattr(paper, "FILLS_PATH", str(tmp_path / "fills.csv"))
    monkeypatch.setattr(paper, "SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    monkeypatch.setattr(paper, "ensure_data", lambda *a, **k: (panel, "synthetic"))
    monkeypatch.setattr(paper, "quality_report", lambda *a, **k: {})
    monkeypatch.setattr(live, "ensure_data", lambda *a, **k: (panel, "synthetic"))
    quality = {
        symbol: {
            "status": "OK",
            "rows": len(df),
            "first": str(df.index[0].date()),
            "last": str(df.index[-1].date()),
            "last_close": float(df["close"].iloc[-1]),
            "adv20_usd": float(df["quote_volume"].tail(20).mean()),
        }
        for symbol, df in panel.items()
    }
    monkeypatch.setattr(live, "quality_report", lambda *a, **k: quality)
    monkeypatch.setattr(live, "JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setattr(live, "MONITOR_JSON", str(tmp_path / "monitor.json"))
    paper.initialize(cfg, start="2026-01-01", force=True)
    paper.run_paper(cfg, quiet=True, refresh=True)
    return tmp_path


def test_snapshot_contains_paper_and_signals(cfg, live_env):
    snapshot = live.collect_snapshot(cfg, include_monitor=False)
    assert snapshot["paper"]["exists"] is True
    assert snapshot["paper"]["start"] == "2026-01-01"
    assert snapshot["data"]["ok_assets"] == len(snapshot["data"]["quality"])
    assert snapshot["regime"]["label"] in {"TREND_UP", "RANGE", "TREND_DOWN", "CRISIS", "WARMUP"}
    assert set(snapshot["signals"]) == set(cfg["universe"])
    assert snapshot["meanrev_enabled"] is False


def test_render_text_has_sections_and_no_nan(cfg, live_env):
    snapshot = live.collect_snapshot(cfg, include_monitor=False)
    text = live.render_text(snapshot, width=100, height=50)
    assert "MERIDIAN" in text
    assert "Régime" in text
    assert "PORTEFEUILLE PAPER" in text
    assert "Aucun ordre réel" in text
    assert "nan" not in text.lower()
    assert "NaN" not in text


def test_dashboard_pagination_keeps_footer(cfg, live_env):
    snapshot = live.collect_snapshot(cfg, include_monitor=False)
    lines = live.format_dashboard(snapshot, width=80, height=20)
    assert len(lines) == 20
    assert "q quitter" in lines[-1][0]
    assert all(len(text) <= 80 for text, _ in lines)


def test_cached_snapshot_avoids_backtest(cfg, live_env, monkeypatch):
    snapshot = live.collect_snapshot(cfg, include_monitor=False)
    assert snapshot["regime"]["label"] in {"TREND_UP", "RANGE", "TREND_DOWN", "CRISIS", "WARMUP"}

    def boom(*args, **kwargs):
        raise AssertionError("le cache snapshot aurait dû éviter le backtest")

    monkeypatch.setattr(live, "Backtest", boom)
    cached = live.collect_snapshot(cfg, include_monitor=False)
    assert cached["regime"] == snapshot["regime"]
    assert cached["signals"] == snapshot["signals"]


def test_next_close_and_countdown():
    now = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)
    target = live._next_daily_close(now)
    assert target == datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
    seconds = live._seconds_until(target.isoformat(), now)
    assert seconds == 11 * 3600
    assert live._fmt_duration(seconds) == "11h 00m 00s"


def test_cached_monitor_is_used(cfg, live_env):
    payload = {"status": "OK", "kill_switch": False, "checks": [], "generated_at": "2026-11-17T13:00:00+00:00"}
    (live_env / "monitor.json").write_text(json.dumps(payload), encoding="utf-8")
    snapshot = live.collect_snapshot(cfg, include_monitor=False)
    assert snapshot["monitor"]["status"] == "OK"
    assert snapshot["monitor"]["cached"] is True


def test_offline_snapshot_reads_only_cache(cfg, panel, live_env, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ensure_data ne doit pas être appelé en mode offline")

    monkeypatch.setattr(live, "ensure_data", boom)
    monkeypatch.setattr(live, "load_panel", lambda *a, **k: panel)
    snapshot = live.collect_snapshot(cfg, include_monitor=False, offline=True)
    assert snapshot["data"]["source"] == "cache (offline)"
    text = live.render_text(snapshot, width=100, offline=True)
    assert "OFFLINE" in text
    assert "télécharger" not in text


def test_child_args_propagate_offline():
    args = live._child_args(5.0, 30.0, 300.0, False, False, 300.0, False, None, True)
    assert "--offline" in args


def test_snapshot_aligns_misaligned_assets(cfg, panel, live_env, monkeypatch):
    mixed = {symbol: df.copy() for symbol, df in panel.items()}
    extra = mixed["BTCUSDT"].iloc[[-1]].copy()
    extra.index = pd.DatetimeIndex(
        [mixed["BTCUSDT"].index[-1] + pd.Timedelta(days=1)]
    )
    mixed["BTCUSDT"] = pd.concat([mixed["BTCUSDT"], extra])
    monkeypatch.setattr(live, "load_panel", lambda *a, **k: mixed)
    snapshot = live.collect_snapshot(cfg, include_monitor=False, offline=True)
    assert snapshot["data"]["last_bar"] == str(panel["ETHUSDT"].index[-1].date())


def test_launch_window_without_gui_returns_failure(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    assert live.launch_window() == 1


def test_run_plain_prints_once(cfg, live_env, monkeypatch, capsys):
    snapshot = live.collect_snapshot(cfg, include_monitor=False)
    monkeypatch.setattr(live, "collect_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(live.sys.stdout, "isatty", lambda: False)
    assert live.run(cfg, plain=True) == 0
    out = capsys.readouterr().out
    assert "MERIDIAN" in out
    assert "Aucun ordre réel" in out
