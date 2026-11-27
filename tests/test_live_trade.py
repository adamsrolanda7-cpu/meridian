"""Mode réel : planification, plafonds et verrous d'exécution."""
from __future__ import annotations

import pytest

import meridian.live_trade as live_trade
from meridian.exchange import AccountSnapshot, Balance
from meridian.live_trade import (
    LiveState,
    LiveTradingError,
    SignalSnapshot,
    assert_execution_allowed,
    clamp_targets,
    plan_orders,
    run_trade,
)


def make_account(
    *,
    equity: float = 100_000.0,
    cash: float = 100_000.0,
    positions: dict[str, float] | None = None,
    prices: dict[str, float] | None = None,
) -> AccountSnapshot:
    positions = positions or {}
    prices = prices or {}
    quote = "USDT"
    balances = {quote: Balance(quote, cash, cash)}
    for asset, qty in positions.items():
        balances[asset] = Balance(asset, qty, qty)
    return AccountSnapshot(
        exchange_id="binance",
        sandbox=True,
        quote=quote,
        balances=balances,
        prices={quote: 1.0, **prices},
        equity=equity,
    )


def test_plan_sells_before_buys(cfg):
    prices = {asset: 100.0 for asset in cfg["universe"]}
    prices["BTCUSDT"] = 50_000.0
    account = make_account(
        cash=50_000.0,
        positions={"BTCUSDT": 1.0},
        prices=prices,
    )
    targets = {asset: 0.0 for asset in cfg["universe"]}
    targets["BTCUSDT"] = 0.25
    targets["ETHUSDT"] = 0.25
    orders, notes = plan_orders(
        account,
        targets,
        cfg,
        max_order_usd=100_000.0,
        max_turnover_usd=100_000.0,
    )
    assert orders[0].asset == "BTCUSDT"
    assert orders[0].side == "SELL"
    assert orders[0].qty == pytest.approx(0.5)
    assert any(o.asset == "ETHUSDT" and o.side == "BUY" for o in orders)
    assert isinstance(notes, list)


def test_max_order_caps_notional(cfg):
    prices = {asset: 100.0 for asset in cfg["universe"]}
    prices["BTCUSDT"] = 50_000.0
    account = make_account(
        cash=50_000.0,
        positions={"BTCUSDT": 1.0},
        prices=prices,
    )
    targets = {asset: 0.0 for asset in cfg["universe"]}
    targets["BTCUSDT"] = 0.0
    orders, _ = plan_orders(
        account,
        targets,
        cfg,
        max_order_usd=1_000.0,
        max_turnover_usd=100_000.0,
    )
    assert orders
    assert orders[0].notional <= 1_000.0 + 1e-6
    assert orders[0].qty == pytest.approx(1_000.0 / 50_000.0)


def test_no_short_when_no_free_balance(cfg):
    prices = {asset: 100.0 for asset in cfg["universe"]}
    account = make_account(cash=100_000.0, positions={}, prices=prices)
    targets = {asset: 0.0 for asset in cfg["universe"]}
    targets["BTCUSDT"] = 0.0
    orders, _ = plan_orders(account, targets, cfg, max_order_usd=1_000.0)
    assert not any(o.side == "SELL" for o in orders)


def test_drawdown_blocks_new_buys(cfg):
    prices = {asset: 100.0 for asset in cfg["universe"]}
    account = make_account(cash=100_000.0, positions={}, prices=prices)
    state = LiveState(peak_equity=200_000.0, last_equity=100_000.0)
    targets = {asset: 0.0 for asset in cfg["universe"]}
    targets["BTCUSDT"] = 0.20
    orders, notes = plan_orders(
        account,
        targets,
        cfg,
        state=state,
        max_order_usd=100_000.0,
    )
    assert not any(o.side == "BUY" for o in orders)
    assert any("drawdown" in note.lower() for note in notes)


def test_confirm_and_env_are_required(monkeypatch):
    settings = {"api_key": "k", "api_secret": "s"}
    with pytest.raises(LiveTradingError, match="--confirm"):
        assert_execution_allowed(True, False, settings)
    monkeypatch.setenv("MERIDIAN_LIVE_CONFIRM", "NO")
    with pytest.raises(LiveTradingError, match="MERIDIAN_LIVE_CONFIRM"):
        assert_execution_allowed(True, True, settings)
    monkeypatch.setenv("MERIDIAN_LIVE_CONFIRM", "YES")
    assert_execution_allowed(True, True, settings)
    assert_execution_allowed(False, False, settings)


def test_clamp_targets_respects_gross_and_asset_caps(cfg):
    targets = {asset: 0.5 for asset in cfg["universe"]}
    clamped = clamp_targets(targets, cfg)
    assert sum(clamped.values()) <= cfg["strategy"]["max_gross"] + 1e-9
    assert max(clamped.values()) <= cfg["strategy"]["max_asset_weight"] + 1e-9


class FakeExchange:
    def __init__(self, account: AccountSnapshot):
        self.account = account
        self.orders: list[dict] = []
        self.closed = False

    def fetch_account(self, assets):
        return self.account

    def create_order(self, asset, side, qty, order_type="market", price=None, params=None):
        self.orders.append(
            {"asset": asset, "side": side, "qty": qty, "type": order_type, "price": price}
        )
        return {"id": f"fake-{len(self.orders)}"}

    def close(self):
        self.closed = True


def _signal(cfg) -> SignalSnapshot:
    targets = {asset: 0.0 for asset in cfg["universe"]}
    targets["BTCUSDT"] = 0.20
    return SignalSnapshot(
        last_bar="2026-11-20",
        regime="TREND_UP",
        targets=targets,
        reasons={"BTCUSDT": "test"},
        stale_days=0,
        source="cache",
        quality={},
    )


def _trade_env(monkeypatch, cfg, tmp_path, fake):
    monkeypatch.setenv("MERIDIAN_EXCHANGE_API_KEY", "key")
    monkeypatch.setenv("MERIDIAN_EXCHANGE_API_SECRET", "secret")
    monkeypatch.setenv("MERIDIAN_EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("MERIDIAN_LIVE_CONFIRM", "YES")
    monkeypatch.setattr(live_trade, "make_exchange", lambda settings: fake)
    monkeypatch.setattr(live_trade, "latest_signal", lambda cfg, refresh=True: _signal(cfg))
    monkeypatch.setattr(live_trade, "LIVE_DIR", str(tmp_path))
    monkeypatch.setattr(live_trade, "LIVE_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(live_trade, "LIVE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(live_trade, "LIVE_JOURNAL_PATH", str(tmp_path / "journal.jsonl"))


def test_run_trade_dry_run_never_sends(cfg, tmp_path, monkeypatch):
    prices = {asset: 100.0 for asset in cfg["universe"]}
    account = make_account(cash=100_000.0, positions={}, prices=prices)
    fake = FakeExchange(account)
    _trade_env(monkeypatch, cfg, tmp_path, fake)
    payload = run_trade(
        cfg,
        execute=False,
        refresh=False,
        max_order_usd=50_000.0,
        max_turnover_usd=50_000.0,
    )
    assert payload["orders"]
    assert fake.orders == []
    assert (tmp_path / "plan.json").exists()
    assert not (tmp_path / "state.json").exists()


def test_run_trade_execute_sends_and_persists(cfg, tmp_path, monkeypatch):
    prices = {asset: 100.0 for asset in cfg["universe"]}
    account = make_account(cash=100_000.0, positions={}, prices=prices)
    fake = FakeExchange(account)
    _trade_env(monkeypatch, cfg, tmp_path, fake)
    payload = run_trade(
        cfg,
        execute=True,
        confirm=True,
        refresh=False,
        max_order_usd=50_000.0,
        max_turnover_usd=50_000.0,
    )
    assert payload["orders_sent"] == 1
    assert len(fake.orders) == 1
    assert fake.orders[0]["asset"] == "BTCUSDT"
    assert fake.orders[0]["side"] == "BUY"
    assert (tmp_path / "state.json").exists()
    assert (tmp_path / "journal.jsonl").exists()
    state = LiveState.load(str(tmp_path / "state.json"))
    assert state is not None
    assert state.last_bar == "2026-11-20"
    assert state.turnover_usd > 0
