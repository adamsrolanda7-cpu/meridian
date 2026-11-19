"""Paper trading : persistance, déterminisme, réconciliation, garde live."""
from __future__ import annotations

import json

import pandas as pd
import pytest

import meridian.paper as paper
from meridian.config import deep_merge


@pytest.fixture()
def paper_env(cfg, panel, tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "PAPER_DIR", str(tmp_path))
    monkeypatch.setattr(paper, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper, "JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setattr(paper, "MEMO_PATH", str(tmp_path / "memo.md"))
    monkeypatch.setattr(paper, "FILLS_PATH", str(tmp_path / "fills.csv"))
    monkeypatch.setattr(paper, "SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    monkeypatch.setattr(paper, "ensure_data", lambda *a, **k: (panel, "synthetic"))
    monkeypatch.setattr(paper, "quality_report", lambda *a, **k: {})
    return tmp_path


def test_state_roundtrip(tmp_path, cfg):
    state = paper.PaperState.new(cfg, "2026-01-01", 100_000.0)
    state.pending_orders = [{"asset": "BTCUSDT", "target": 0.2, "reason": "test"}]
    state.save(str(tmp_path / "state.json"))
    loaded = paper.PaperState.load(str(tmp_path / "state.json"))
    assert loaded.start == state.start
    assert loaded.initial_equity == state.initial_equity
    assert loaded.pending_map() == {"BTCUSDT": 0.2}
    assert loaded.config_hash == paper.config_hash(cfg)


def test_paper_replay_and_persistence(cfg, paper_env):
    state = paper.initialize(cfg, start="2026-01-01", force=True)
    assert state.start == "2026-01-01"
    first = paper.run_paper(cfg, quiet=True, refresh=True)
    assert first["last_bar"] >= "2026-01-01"
    assert first["data_source"] == "synthetic"
    loaded = paper.PaperState.load()
    assert loaded.last_bar == first["last_bar"]
    assert loaded.equity == pytest.approx(first["equity"])
    assert len(loaded.runs) == 1
    assert (paper_env / "memo.md").exists()
    assert (paper_env / "journal.jsonl").exists()
    assert (paper_env / "snapshot.json").exists()

    second = paper.run_paper(cfg, quiet=True, refresh=True)
    assert second["last_bar"] == first["last_bar"]
    assert second["equity"] == pytest.approx(first["equity"])
    assert second["new_fills"] == 0
    loaded = paper.PaperState.load()
    assert len(loaded.runs) == 2
    assert not second["reconciliation"]

    lines = (paper_env / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[-1])["last_bar"] == first["last_bar"]


def test_paper_refuses_config_drift(cfg, paper_env):
    paper.initialize(cfg, start="2026-01-01", force=True)
    changed = deep_merge(cfg, {"strategy": {"target_vol": 0.25}})
    with pytest.raises(paper.PaperError, match="configuration a changé"):
        paper.run_paper(changed, quiet=True, refresh=True)


def test_paper_requires_init(cfg, paper_env):
    with pytest.raises(paper.PaperError, match="aucun état papier"):
        paper.run_paper(cfg, quiet=True, refresh=True)


def test_live_mode_is_refused(cfg, paper_env):
    live = deep_merge(cfg, {"execution": {"mode": "live", "broker": "binance"}})
    with pytest.raises(paper.LiveTradingDisabled):
        paper.assert_paper_only(live)
    paper.assert_paper_only(cfg)


def test_summary_mentions_no_real_orders(cfg, paper_env):
    paper.initialize(cfg, start="2026-01-01", force=True)
    summary = paper.run_paper(cfg, quiet=True, refresh=True)
    text = paper.format_paper_summary(summary)
    assert "Aucun ordre réel" in text
    assert "MERIDIAN PAPER" in text


def test_verify_matches_replay(cfg, paper_env):
    paper.initialize(cfg, start="2026-01-01", force=True)
    paper.run_paper(cfg, quiet=True, refresh=True)
    audit = paper.verify(cfg, refresh=True)
    assert audit["ok"], audit["issues"]
    assert audit["state"]["equity"] == pytest.approx(audit["replay"]["equity"])
    assert audit["replay"]["start"] == "2026-01-01"
    text = paper.format_verification(audit)
    assert "vérification · OK" in text


def test_verify_detects_tampered_equity(cfg, paper_env):
    paper.initialize(cfg, start="2026-01-01", force=True)
    paper.run_paper(cfg, quiet=True, refresh=True)
    state = paper.PaperState.load()
    state.equity += 1_000.0
    state.save()
    audit = paper.verify(cfg, refresh=True)
    assert not audit["ok"]
    assert any("equity divergente" in issue for issue in audit["issues"])
    assert "DIVERGENCE" in paper.format_verification(audit)
