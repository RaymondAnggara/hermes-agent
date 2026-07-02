"""Tests for prediction resolution + edge-report assembly (resolution.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plugins.trading.marketdata import Candle
from plugins.trading.resolution import (
    price_at_or_before,
    resolve_predictions,
    strategy_report,
)

BASE = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def _candles(n=20, step_h=4):
    # price = 100 + i, one candle every step_h hours from BASE.
    return [
        Candle(int((BASE + timedelta(hours=step_h * i)).timestamp() * 1000), 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i)
        for i in range(n)
    ]


def _pred(action="buy", at=BASE, raw_p=0.7, symbol="BTC"):
    return {"decided_at": at.isoformat(), "action": action, "raw_p": raw_p, "symbol": symbol}


# ---- price lookup ---------------------------------------------------------


def test_price_at_or_before_picks_latest_not_after():
    c = _candles()
    ts = int((BASE + timedelta(hours=10)).timestamp() * 1000)  # between candle 2 (8h) and 3 (12h)
    assert price_at_or_before(c, ts) == 102.0  # candle 2 = 100+2


def test_price_at_or_before_none_when_all_after():
    c = _candles()
    assert price_at_or_before(c, int((BASE - timedelta(days=5)).timestamp() * 1000)) is None


# ---- resolution -----------------------------------------------------------


def test_resolves_once_horizon_elapsed():
    c = _candles()
    resolved, pending = resolve_predictions(
        [_pred()], c, horizon_hours=24.0, now=BASE + timedelta(hours=80)
    )
    assert pending == 0 and len(resolved) == 1
    assert resolved[0]["price_at"] == 100.0            # candle at BASE
    assert resolved[0]["price_after"] == 106.0         # candle at BASE+24h (24/4=6)


def test_pending_when_horizon_not_elapsed():
    c = _candles()
    resolved, pending = resolve_predictions(
        [_pred()], c, horizon_hours=24.0, now=BASE + timedelta(hours=10)
    )
    assert resolved == [] and pending == 1


def test_pending_when_outside_price_history():
    c = _candles()
    old = _pred(at=BASE - timedelta(days=10))
    resolved, pending = resolve_predictions([old], c, now=BASE + timedelta(hours=80))
    assert resolved == [] and pending == 1


# ---- report assembly ------------------------------------------------------


class _StubStore:
    def __init__(self, preds):
        self._preds = preds

    def list_predictions(self, symbol):
        return [p for p in self._preds if p["symbol"] == symbol.upper()]


def test_report_no_data_is_honest():
    rep = strategy_report(_StubStore([]), {"BTC": _candles()}, ["BTC"], now=BASE + timedelta(hours=80))
    assert rep["overall"]["n"] == 0
    assert rep["overall"]["edge"] is None
    assert "No resolved predictions" in rep["overall"]["note"]


def test_report_scores_resolved_predictions():
    # A correct bullish call in a rising series: strategy wins, beats nothing to fear.
    store = _StubStore([_pred(action="buy")])
    rep = strategy_report(store, {"BTC": _candles()}, ["BTC"], now=BASE + timedelta(hours=80))
    ov = rep["overall"]
    assert ov["n"] == 1 and ov["wins"] == 1
    assert rep["per_symbol"]["BTC"]["n"] == 1
    assert rep["horizon_hours"] == 24.0
