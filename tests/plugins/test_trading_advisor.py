"""Tests for the read-only market read assembly (advisor.py).

Focus: the honesty contract (always n_samples + calibrated + a caveat while
uncalibrated), the directional lean, signal recording tagged to the data source,
and graceful handling of a too-short series.
"""

from __future__ import annotations

import pytest

from plugins.trading.advisor import market_read
from plugins.trading.marketdata import DATA_SOURCE, Candle
from plugins.trading.store import TradingStore


def _candles(closes: list[float]) -> list[Candle]:
    return [Candle(1_700_000_000_000 + i * 14_400_000, c, c, c, c) for i, c in enumerate(closes)]


def _ramp(start: float, step: float, n: int) -> list[float]:
    return [start + step * i for i in range(n)]


@pytest.fixture
def store(tmp_path):
    s = TradingStore.open(tmp_path / "trading.db")
    yield s
    s.close()


def test_uptrend_reads_bullish_and_records_signals(store):
    read = market_read("btc", _candles(_ramp(100, 1.0, 40)), store)
    assert read["symbol"] == "BTC"
    assert read["lean"] == "bullish"
    assert read["confidence_p"] > 0.5
    assert read["last_price"] == pytest.approx(139.0)
    assert {s["name"] for s in read["signals"]} == {"trend", "momentum"}
    # Signals were persisted, tagged with the data source (train/serve-skew guard).
    latest = store.latest_signals("BTC")
    assert {r["signal_type"] for r in latest} == {"trend", "momentum"}
    assert all(r["source_ref"] == DATA_SOURCE for r in latest)


def test_downtrend_reads_bearish(store):
    read = market_read("BTC", _candles(_ramp(200, -1.0, 40)), store)
    assert read["lean"] == "bearish"
    assert read["confidence_p"] < 0.5


def test_honesty_contract_present_and_uncalibrated(store):
    read = market_read("ETH", _candles(_ramp(100, 1.0, 40)), store)
    assert read["n_samples"] == 0          # no resolved trades yet
    assert read["calibrated"] is False      # no calibration bands yet
    assert read["note"] and "UNCALIBRATED" in read["note"]
    # Uncalibrated => reported confidence is exactly the raw model number.
    assert read["confidence_p"] == read["raw_confidence_p"]


def test_short_series_is_neutral_with_all_signals_unavailable(store):
    read = market_read("BTC", _candles([100.0, 101.0, 102.0]), store)
    assert read["signals"] == []
    assert set(read["unavailable_signals"]) == {"trend", "momentum"}
    assert read["lean"] == "neutral"
    assert read["confidence_p"] == pytest.approx(0.5)  # no signals => coin flip


def test_record_false_does_not_write(store):
    market_read("BTC", _candles(_ramp(100, 1.0, 40)), store, record=False)
    assert store.latest_signals("BTC") == []


def test_works_without_store():
    # No store => no n_samples/bands, but still a valid (uncalibrated) read.
    read = market_read("BTC", _candles(_ramp(100, 1.0, 40)), None)
    assert read["calibrated"] is False and read["n_samples"] == 0
    assert read["lean"] == "bullish"
