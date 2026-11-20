"""Tests du prototype ``quant_agent``.

Ces tests restent légers : ils ne lancent pas de backtest et n'importent pas
pandas. L'intégration réelle est testée via les commandes CLI sur le cache.
"""
from __future__ import annotations

import json
import os

import pytest

from quant_agent import formatting, presets
from quant_agent.agent import Agent
from quant_agent.policy import Policy
from quant_agent.providers import MockProvider, ProviderResponse, ToolCall
from quant_agent.quotas import QuotaExceeded, QuotaManager
from quant_agent.store import (
    RunStore,
    compute_run_id,
    summary_from_payload,
)
from quant_agent.tools import (
    TOOL_DEFS,
    QuantTools,
    ToolError,
    ToolRegistry,
    validate_arguments,
)


def _definition(name: str) -> dict:
    return next(item for item in TOOL_DEFS if item["name"] == name)


def test_two_strategy_presets_are_valid_and_distinct():
    names = list(presets.STRATEGIES)
    assert names == ["trend_momentum", "mean_reversion"]
    trend = presets.build_config("trend_momentum")
    meanrev = presets.build_config("mean_reversion")
    assert trend["strategy"]["meanrev"]["enabled"] is False
    assert trend["strategy"]["xsmom"]["enabled"] is True
    assert meanrev["strategy"]["meanrev"]["enabled"] is True
    assert meanrev["strategy"]["xsmom"]["enabled"] is False
    assert presets.DEFAULT_STRATEGY == "trend_momentum"


def test_run_id_is_stable_and_data_sensitive():
    kwargs = {
        "strategy": "trend_momentum",
        "cfg": {"universe": ["BTCUSDT"], "strategy": {"target_vol": 0.22}},
        "start": "2024-01-01",
        "end": "2025-01-01",
        "initial_equity": 100_000.0,
        "cost_multiplier": 1.0,
        "data_snapshot": {"BTCUSDT": {"rows": 365, "last": "2025-01-01"}},
    }
    first = compute_run_id(**kwargs)
    second = compute_run_id(**kwargs)
    assert first == second
    assert first.startswith("bt_") and len(first) == 15
    changed = dict(kwargs)
    changed["data_snapshot"] = {"BTCUSDT": {"rows": 366, "last": "2025-01-02"}}
    assert compute_run_id(**changed) != first


def test_run_store_roundtrip(tmp_path):
    store = RunStore(root=str(tmp_path))
    run_id = "bt_" + "a" * 12
    payload = {
        "run_id": run_id,
        "created_at": "2026-11-20T00:00:00+00:00",
        "strategy": "trend_momentum",
        "label": "Trend",
        "start": "2024-01-01",
        "end": "2025-01-01",
        "initial_equity": 100_000.0,
        "data": {"last_bar": "2025-01-01"},
        "metrics": {"cagr": 0.1, "sharpe": 1.0},
        "warnings": [],
    }
    assert store.save(payload) == run_id
    loaded = store.load(run_id)
    assert loaded is not None
    assert loaded["metrics"]["sharpe"] == 1.0
    assert store.latest()["run_id"] == run_id
    summary = summary_from_payload(loaded)
    assert summary["run_id"] == run_id
    assert summary["metrics"]["cagr"] == 0.1
    assert store.load("bt_doesnotexist") is None


def test_mock_provider_routes_to_tools():
    provider = MockProvider()
    cases = [
        ("Quelles stratégies as-tu ?", "list_strategies"),
        ("Fais un backtest trend momentum sur 2 ans", "run_backtest"),
        ("Montre le dernier backtest", "get_backtest"),
        ("Explique le Sharpe", "explain_metric"),
        ("Combien de backtests puis-je faire ?", "get_usage"),
        ("Où en est le portefeuille paper ?", "get_paper_portfolio"),
        ("Quel est le risque actuel ?", "get_risk_report"),
    ]
    for text, expected in cases:
        provider.begin_turn(text)
        response = provider.complete([], [])
        assert response.tool_calls, text
        assert response.tool_calls[0].name == expected


def test_mock_provider_extracts_dates_and_compare():
    provider = MockProvider()
    provider.begin_turn("Fais un backtest mean reversion depuis 2023")
    response = provider.complete([], [])
    call = response.tool_calls[0]
    assert call.name == "run_backtest"
    assert call.arguments["strategy"] == "mean_reversion"
    assert call.arguments["start"] == "2023-01-01"

    provider.begin_turn("Compare trend et mean reversion sur 2 ans")
    response = provider.complete([], [])
    assert [call.name for call in response.tool_calls] == [
        "run_backtest",
        "run_backtest",
    ]
    assert {call.arguments["strategy"] for call in response.tool_calls} == {
        "trend_momentum",
        "mean_reversion",
    }
    assert all(call.arguments["lookback_days"] == 730 for call in response.tool_calls)


def test_mock_provider_compares_existing_runs():
    provider = MockProvider()
    provider.begin_turn("Compare bt_aaaaaaaaaaaa et bt_bbbbbbbbbbbb")
    response = provider.complete([], [])
    assert response.tool_calls[0].name == "compare_backtests"
    assert response.tool_calls[0].arguments["run_id_a"] == "bt_aaaaaaaaaaaa"


def test_mock_provider_refuses_real_trading():
    provider = MockProvider()
    provider.begin_turn("Passe un ordre réel avec du levier")
    response = provider.complete([], [])
    assert not response.tool_calls
    assert "trading réel" in response.text


def test_argument_validation_rejects_bad_values():
    spec = _definition("run_backtest")
    with pytest.raises(ToolError):
        validate_arguments(spec, {"strategy": "inconnue"})
    with pytest.raises(ToolError):
        validate_arguments(spec, {"lookback_days": 2})
    with pytest.raises(ToolError):
        validate_arguments(spec, {"unknown": 1})
    with pytest.raises(ToolError):
        validate_arguments(_definition("compare_backtests"), {"run_id_a": "x"})


def test_tool_registry_unknown_tool(tmp_path):
    tools = QuantTools(root=str(tmp_path))
    registry = ToolRegistry(tools)
    result = registry.call("nope", {})
    assert result["ok"] is False
    assert "inconnu" in result["error"]
    assert len(registry.schemas()) == len(TOOL_DEFS)


def test_quota_manager(tmp_path):
    quotas = QuotaManager(
        root=str(tmp_path),
        limits={"messages": 2, "backtests": 1, "tokens": 10},
    )
    quotas.consume("messages")
    quotas.consume("messages")
    with pytest.raises(QuotaExceeded):
        quotas.consume("messages")
    quotas.consume("backtests")
    with pytest.raises(QuotaExceeded):
        quotas.consume("backtests")
    quotas.add_tokens(11)
    with pytest.raises(QuotaExceeded):
        quotas.check("tokens")
    snapshot = quotas.snapshot()
    assert snapshot["used"]["messages"] == 2
    assert snapshot["remaining"]["messages"] == 0
    quotas.reset()
    assert quotas.snapshot()["used"]["messages"] == 0


class _FakeProvider:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def begin_turn(self, user_text):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                tool_calls=[ToolCall("call_1", "list_strategies", {})],
                usage={"total_tokens": 5},
            )
        return ProviderResponse(text="Réponse finale.", usage={"total_tokens": 5})


def test_agent_loop_and_audit(tmp_path):
    quotas = QuotaManager(root=str(tmp_path))
    tools = QuantTools(root=str(tmp_path), quotas=quotas)
    registry = ToolRegistry(tools)
    agent = Agent(
        _FakeProvider(),
        registry,
        policy=Policy("fast"),
        quotas=quotas,
        root=str(tmp_path),
    )
    result = agent.ask("liste les stratégies")
    assert result.answer == "Réponse finale."
    assert result.traces[0].name == "list_strategies"
    assert result.traces[0].ok is True
    assert result.tokens == 10
    audit_path = os.path.join(tmp_path, "runs", "agent", "audit.jsonl")
    assert os.path.exists(audit_path)
    entry = json.loads(open(audit_path, encoding="utf-8").read().splitlines()[0])
    assert entry["tool"] == "list_strategies"
    assert entry["ok"] is True


def test_comparison_prefers_less_negative_drawdown():
    payload = {
        "runs": [
            {
                "run_id": "bt_aaaaaaaaaaaa",
                "label": "A",
                "metrics": {"max_drawdown": -0.10, "sharpe": 1.0, "cagr": 0.2},
            },
            {
                "run_id": "bt_bbbbbbbbbbbb",
                "label": "B",
                "metrics": {"max_drawdown": -0.20, "sharpe": 0.5, "cagr": 0.1},
            },
        ],
        "deltas": {},
    }
    text = formatting.format_comparison(payload)
    assert "Drawdown le plus faible : bt_aaaaaaaaaaaa" in text
