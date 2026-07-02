"""Tests for the daily forecast-log tick (daily.py)."""

from __future__ import annotations

import pytest

from plugins.trading.daily import record_daily_reads
from plugins.trading.marketdata import Candle, MarketDataError
from plugins.trading.risk_gate import Caps
from plugins.trading.store import TradingStore

V1_CAPS = Caps(20.0, 40.0, 100.0, ("BTC", "ETH"), 90.0)


def _candles(closes):
    return [Candle(1_700_000_000_000 + i * 14_400_000, c, c, c, c) for i, c in enumerate(closes)]


@pytest.fixture
def store(tmp_path):
    s = TradingStore.open(tmp_path / "trading.db")
    yield s
    s.close()


def test_records_signals_and_directional_prediction(store):
    up = _candles([100.0 + i for i in range(40)])   # bullish
    flat = _candles([100.0] * 40)                    # neutral
    def fake_fetch(sym, **kw):
        return up if sym == "BTC" else flat
    res = record_daily_reads(store, V1_CAPS, ["BTC", "ETH"], fetch=fake_fetch)

    assert res["BTC"]["ok"] and res["BTC"]["lean"] == "bullish"
    assert res["BTC"]["prediction_id"] is not None          # directional → forecast logged
    assert res["ETH"]["lean"] == "neutral"
    assert res["ETH"]["prediction_id"] is None              # neutral → abstains, no prediction

    # Signals are recorded for BOTH symbols (unbiased sampling); a prediction only
    # for the directional one.
    assert {r["signal_type"] for r in store.latest_signals("BTC")} == {"trend", "momentum"}
    assert {r["signal_type"] for r in store.latest_signals("ETH")} == {"trend", "momentum"}
    assert len(store.list_predictions("BTC")) == 1
    assert store.list_predictions("ETH") == []


def test_records_no_trades(store):
    up = _candles([100.0 + i for i in range(40)])
    record_daily_reads(store, V1_CAPS, ["BTC"], fetch=lambda s, **k: up)
    assert store.recent_trades(10) == []  # data generation NEVER fills


def test_fetch_error_is_captured_not_raised(store):
    def boom(sym, **kw):
        raise MarketDataError("no route")
    res = record_daily_reads(store, V1_CAPS, ["BTC"], fetch=boom)
    assert res["BTC"]["ok"] is False and "no route" in res["BTC"]["error"]
