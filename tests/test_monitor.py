"""Suivi opérationnel : kill criteria, réconciliation et absence de NaN."""
from __future__ import annotations

import json
import os

import pytest

import meridian.monitor as monitor
import meridian.paper as paper


@pytest.fixture()
def monitor_env(cfg, panel, tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "PAPER_DIR", str(tmp_path))
    monkeypatch.setattr(paper, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper, "JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setattr(paper, "MEMO_PATH", str(tmp_path / "memo.md"))
    monkeypatch.setattr(paper, "FILLS_PATH", str(tmp_path / "fills.csv"))
    monkeypatch.setattr(paper, "SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    monkeypatch.setattr(paper, "ensure_data", lambda *a, **k: (panel, "synthetic"))
    monkeypatch.setattr(paper, "quality_report", lambda *a, **k: {})
    monkeypatch.setattr(monitor, "ensure_data", lambda *a, **k: (panel, "synthetic"))
    monkeypatch.setattr(monitor, "quality_report", lambda *a, **k: {})
    return tmp_path


def test_monitor_reports_checks_and_writes(cfg, monitor_env):
    paper.initialize(cfg, start="2026-01-01", force=True)
    paper.run_paper(cfg, quiet=True, refresh=True)
    payload = monitor.evaluate(cfg, refresh=True)
    assert payload["status"] in {"OK", "WATCH", "ALERT"}
    names = {check["name"] for check in payload["checks"]}
    assert {"fraîcheur données", "drawdown", "Sharpe glissant", "exposition"} <= names
    assert payload["paper"]["start"] == "2026-01-01"
    assert payload["attribution"]["all"]["trades"] >= 0

    json_path, md_path = monitor.write(
        payload, str(monitor_env / "monitor.json"), str(monitor_env / "monitor.md")
    )
    assert os.path.exists(json_path) and os.path.exists(md_path)
    raw = open(json_path, encoding="utf-8").read()
    assert "NaN" not in raw
    assert "MERIDIAN" in open(md_path, encoding="utf-8").read()
    assert "Aucun ordre réel" in monitor.format_monitor(payload)


def test_monitor_without_paper_is_alert(cfg, monitor_env):
    payload = monitor.evaluate(cfg, refresh=True)
    assert payload["status"] == "ALERT"
    assert payload["kill_switch"] is True
    assert any(check["name"] == "état papier" for check in payload["checks"])


def test_monitor_flags_config_drift(cfg, monitor_env):
    paper.initialize(cfg, start="2026-01-01", force=True)
    paper.run_paper(cfg, quiet=True, refresh=True)
    from meridian.config import deep_merge

    changed = deep_merge(cfg, {"strategy": {"target_vol": 0.25}})
    payload = monitor.evaluate(changed, refresh=True)
    mandate = [c for c in payload["checks"] if c["name"] == "mandat"][0]
    assert mandate["status"] == "ALERT"
    assert payload["kill_switch"] is True
