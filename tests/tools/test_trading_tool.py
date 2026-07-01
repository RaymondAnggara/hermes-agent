"""Tests for the service-gated ``trade`` model tool (tools/trading_tool.py).

The tool wiring is the only new surface in Phase 5b build step 3b — the order
logic itself is covered under tests/plugins/. These tests assert the two gates
(check_fn requires enabled+caps; caps clamp to HARD_CEILING) and that the handler
runs the full paper path fail-closed: a rejection changes nothing, a fill writes
the audit trade, and both come back as structured JSON.
"""

from __future__ import annotations

import json

import pytest

import tools.trading_tool as tt
from plugins.trading import HARD_CEILING
from plugins.trading.marketdata import Candle, MarketDataError
from plugins.trading.store import TradingStore
from tools.registry import registry


def _candles(closes):
    return [Candle(1_700_000_000_000 + i * 14_400_000, c, c, c, c) for i, c in enumerate(closes)]


def _uptrend():
    return _candles([100.0 + i for i in range(40)])

V1_CAPS_CFG = {
    "enabled": True,
    "caps": {
        "max_order_notional": 20,
        "max_position_per_symbol": 40,
        "max_period_notional": 100,
        "allowed_symbols": ["BTC", "ETH"],
        "hard_ceiling": 90,
    },
}


@pytest.fixture
def enabled(monkeypatch):
    """Point the tool at a fully-configured, enabled trading section."""
    monkeypatch.setattr(tt, "_trading_config", lambda: dict(V1_CAPS_CFG))
    return V1_CAPS_CFG


# ---- registration ---------------------------------------------------------


def test_trade_tool_is_registered_under_trading_toolset():
    entry = registry.get_entry("trade")
    assert entry is not None
    assert entry.toolset == "trading"
    assert entry.check_fn is tt._check_trading_enabled


# ---- check_fn service gate ------------------------------------------------


def test_check_disabled_when_no_trading_config(monkeypatch):
    monkeypatch.setattr(tt, "_trading_config", dict)
    assert tt._check_trading_enabled() is False


def test_check_disabled_when_enabled_false(monkeypatch):
    monkeypatch.setattr(tt, "_trading_config", lambda: {"enabled": False, "caps": V1_CAPS_CFG["caps"]})
    assert tt._check_trading_enabled() is False


def test_check_disabled_when_enabled_but_caps_missing(monkeypatch):
    # Fail-closed: enabled with no usable caps must still hide the tool.
    monkeypatch.setattr(tt, "_trading_config", lambda: {"enabled": True})
    assert tt._check_trading_enabled() is False


def test_check_enabled_when_enabled_and_caps_present(enabled):
    assert tt._check_trading_enabled() is True


# ---- caps loading + clamping ----------------------------------------------


def test_load_caps_returns_none_without_caps():
    assert tt._load_caps({"enabled": True}) is None


def test_load_caps_returns_none_on_malformed_caps():
    assert tt._load_caps({"caps": {"max_order_notional": "abc"}}) is None


def test_load_caps_clamps_to_hard_ceiling():
    # A too-loose config cannot exceed the absolute in-code ceiling.
    caps = tt._load_caps(
        {
            "caps": {
                "max_order_notional": 10_000,
                "max_position_per_symbol": 10_000,
                "max_period_notional": 100,
                "allowed_symbols": ["btc"],
                "hard_ceiling": 10_000,
            }
        }
    )
    assert caps is not None
    assert caps.hard_ceiling == HARD_CEILING
    assert caps.max_order_notional <= HARD_CEILING
    assert caps.max_position_per_symbol <= HARD_CEILING
    assert caps.allowed_symbols == ("BTC",)  # normalized upper-case


# ---- handler: full paper path ---------------------------------------------


def test_handler_happy_path_fills_and_writes_audit(enabled):
    out = json.loads(tt._handle_trade({"order": "buy 1 BTC limit 10"}))
    assert out["ok"] is True and out["status"] == "filled"
    assert out["mode"] == "paper"
    assert out["symbol"] == "BTC" and out["fill_px"] == 10.0
    # The fill is durable in the audit store.
    store = TradingStore.open()
    try:
        assert len(store.recent_trades(10)) == 1
    finally:
        store.close()


def test_handler_over_cap_rejects_and_writes_nothing(enabled):
    # 5 BTC @ 10 = 50 notional > max_order_notional 20 → rejected at the gate.
    out = json.loads(tt._handle_trade({"order": "buy 5 BTC limit 10"}))
    assert out["ok"] is False and out["status"] == "rejected"
    assert out["stage"] == "risk_gate"
    store = TradingStore.open()
    try:
        assert store.recent_trades(10) == []
    finally:
        store.close()


def test_handler_disallowed_symbol_rejects(enabled):
    out = json.loads(tt._handle_trade({"order": "buy 0.001 DOGE limit 10"}))
    assert out["ok"] is False and out["status"] == "rejected"


def test_handler_bad_command_rejects_at_parse(enabled):
    out = json.loads(tt._handle_trade({"order": "buy BTC"}))
    assert out["ok"] is False and out["status"] == "rejected"
    assert out["stage"] == "parse"


def test_handler_market_order_rejected_without_price_feed(enabled):
    # No live price feed in paper → market orders can't be priced → rejected.
    out = json.loads(tt._handle_trade({"order": "buy 0.001 BTC market"}))
    assert out["ok"] is False and out["status"] == "rejected"


def test_handler_missing_order_arg_is_tool_error(enabled):
    out = json.loads(tt._handle_trade({}))
    assert "error" in out


def test_handler_fail_closed_when_caps_unconfigured(monkeypatch):
    monkeypatch.setattr(tt, "_trading_config", lambda: {"enabled": True})
    out = json.loads(tt._handle_trade({"order": "buy 1 BTC limit 10"}))
    assert "error" in out


# ---- market_read tool -----------------------------------------------------


def test_market_read_registered_under_trading_toolset():
    entry = registry.get_entry("market_read")
    assert entry is not None
    assert entry.toolset == "trading"
    assert entry.check_fn is tt._check_trading_enabled


def test_market_read_happy_path(enabled, monkeypatch):
    monkeypatch.setattr(tt, "fetch_ohlc", lambda symbol, **kw: _uptrend())
    out = json.loads(tt._handle_market_read({"symbol": "btc"}))
    assert out["symbol"] == "BTC"
    assert out["lean"] == "bullish"
    assert out["calibrated"] is False and "UNCALIBRATED" in out["note"]
    assert {s["name"] for s in out["signals"]} == {"trend", "momentum"}


def test_market_read_rejects_symbol_outside_allowlist(enabled, monkeypatch):
    # Should never even hit the network for a disallowed symbol.
    def _boom(*a, **k):
        raise AssertionError("fetch must not be called for a disallowed symbol")

    monkeypatch.setattr(tt, "fetch_ohlc", _boom)
    out = json.loads(tt._handle_market_read({"symbol": "DOGE"}))
    assert "error" in out


def test_market_read_fails_closed_on_data_error(enabled, monkeypatch):
    def _raise(symbol, **kw):
        raise MarketDataError("no route to CoinGecko")

    monkeypatch.setattr(tt, "fetch_ohlc", _raise)
    out = json.loads(tt._handle_market_read({"symbol": "BTC"}))
    assert "error" in out and "market data unavailable" in out["error"]


def test_market_read_requires_symbol(enabled):
    out = json.loads(tt._handle_market_read({}))
    assert "error" in out


# ---- propose_trade tool ---------------------------------------------------


def test_propose_trade_registered_under_trading_toolset():
    entry = registry.get_entry("propose_trade")
    assert entry is not None
    assert entry.toolset == "trading"
    assert entry.check_fn is tt._check_trading_enabled


def test_propose_trade_bullish_proposes_and_records(enabled, monkeypatch):
    monkeypatch.setattr(tt, "fetch_ohlc", lambda symbol, **kw: _uptrend())
    out = json.loads(tt._handle_propose_trade({"symbol": "BTC"}))
    assert out["proposal"] is not None
    assert out["proposal"]["side"] == "buy"
    assert out["prediction_id"] is not None
    # The proposal does NOT place a trade — audit still empty.
    store = TradingStore.open()
    try:
        assert store.recent_trades(10) == []
    finally:
        store.close()


def test_propose_trade_symbol_outside_allowlist(enabled, monkeypatch):
    monkeypatch.setattr(tt, "fetch_ohlc", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    out = json.loads(tt._handle_propose_trade({"symbol": "DOGE"}))
    assert "error" in out


def test_propose_trade_fails_closed_on_data_error(enabled, monkeypatch):
    def _raise(symbol, **kw):
        raise MarketDataError("no route")

    monkeypatch.setattr(tt, "fetch_ohlc", _raise)
    out = json.loads(tt._handle_propose_trade({"symbol": "BTC"}))
    assert "error" in out


# ---- trade tool: prediction linking ---------------------------------------


def test_trade_links_prediction_id(enabled):
    store = TradingStore.open()
    try:
        pred_id = store.record_prediction("BTC", 0.5, 0.6, 0.6, "buy")
    finally:
        store.close()
    out = json.loads(tt._handle_trade({"order": "buy 1 BTC limit 10", "prediction_id": pred_id}))
    assert out["ok"] and out["prediction_id"] == pred_id
    store = TradingStore.open()
    try:
        row = store.conn.execute(
            "SELECT prediction_id FROM trades WHERE id = ?", (out["trade_id"],)
        ).fetchone()
        assert row["prediction_id"] == pred_id
    finally:
        store.close()


def test_trade_rejects_non_integer_prediction_id(enabled):
    out = json.loads(tt._handle_trade({"order": "buy 1 BTC limit 10", "prediction_id": "abc"}))
    assert "error" in out
