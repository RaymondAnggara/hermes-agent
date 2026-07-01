"""Tests for the pure market-data signals (signals.py).

Invariants, not snapshots: an uptrend leans positive, a downtrend negative, a
flat series ~0, opinions stay clamped to [-1, 1], and insufficient data yields
None (unavailable) rather than a fabricated neutral 0.
"""

from __future__ import annotations

from plugins.trading.marketdata import Candle
from plugins.trading.signals import (
    compute_signals,
    momentum_signal,
    trend_signal,
)


def _candles(closes: list[float]) -> list[Candle]:
    # ts strictly increasing; OHLC collapsed to the close (signals use closes).
    return [Candle(1_700_000_000_000 + i * 14_400_000, c, c, c, c) for i, c in enumerate(closes)]


def _ramp(start: float, step: float, n: int) -> list[float]:
    return [start + step * i for i in range(n)]


# ---- trend ----------------------------------------------------------------


def test_trend_uptrend_is_bullish():
    sig = trend_signal(_candles(_ramp(100, 1.0, 40)))
    assert sig is not None and sig.name == "trend"
    assert sig.s > 0.0


def test_trend_downtrend_is_bearish():
    sig = trend_signal(_candles(_ramp(200, -1.0, 40)))
    assert sig is not None and sig.s < 0.0


def test_trend_flat_is_near_zero():
    sig = trend_signal(_candles([100.0] * 40))
    assert sig is not None and abs(sig.s) < 1e-9


def test_trend_insufficient_data_is_none():
    assert trend_signal(_candles([100.0] * 5)) is None  # < slow window


def test_trend_opinion_is_clamped():
    # A violent uptrend would overshoot ±1 without clamping.
    sig = trend_signal(_candles(_ramp(100, 50.0, 40)))
    assert sig is not None and -1.0 <= sig.s <= 1.0
    assert sig.s == 1.0


# ---- momentum -------------------------------------------------------------


def test_momentum_positive_return_is_bullish():
    sig = momentum_signal(_candles(_ramp(100, 2.0, 20)))
    assert sig is not None and sig.name == "momentum" and sig.s > 0.0


def test_momentum_negative_return_is_bearish():
    sig = momentum_signal(_candles(_ramp(200, -2.0, 20)))
    assert sig is not None and sig.s < 0.0


def test_momentum_insufficient_data_is_none():
    assert momentum_signal(_candles([100.0] * 5)) is None


# ---- compute_signals ------------------------------------------------------


def test_compute_signals_reports_available_and_unavailable():
    signals, unavailable = compute_signals(_candles(_ramp(100, 1.0, 40)))
    names = {s.name for s in signals}
    assert names == {"trend", "momentum"}
    assert unavailable == []


def test_compute_signals_flags_unavailable_on_short_series():
    signals, unavailable = compute_signals(_candles([100.0] * 3))
    assert signals == []
    assert set(unavailable) == {"trend", "momentum"}
