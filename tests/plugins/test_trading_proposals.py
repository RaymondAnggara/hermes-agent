"""Tests for read → proposed paper order (proposals.py, build step 4b).

Invariants: a directional read proposes a cap-respecting sized order AND records
the forecast; a neutral read abstains and records nothing; a directional read
whose sized order the risk gate blocks still records the forecast but proposes no
order (calibration must see every call, we just can't fund them all).
"""

from __future__ import annotations

import pytest

from plugins.trading.marketdata import Candle
from plugins.trading.proposals import MIN_PROPOSAL_NOTIONAL, propose_order
from plugins.trading.risk_gate import Caps
from plugins.trading.store import TradingStore

V1_CAPS = Caps(20.0, 40.0, 100.0, ("BTC", "ETH"), 90.0)


def _candles(closes: list[float]) -> list[Candle]:
    return [Candle(1_700_000_000_000 + i * 14_400_000, c, c, c, c) for i, c in enumerate(closes)]


def _ramp(start: float, step: float, n: int = 40) -> list[float]:
    return [start + step * i for i in range(n)]


@pytest.fixture
def store(tmp_path):
    s = TradingStore.open(tmp_path / "trading.db")
    yield s
    s.close()


def _npredictions(store) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]


def test_bullish_read_proposes_buy_within_caps_and_records_prediction(store):
    res = propose_order("BTC", _candles(_ramp(100, 2.0)), store, V1_CAPS)
    prop = res["proposal"]
    assert prop is not None
    assert prop["side"] == "buy"
    assert prop["symbol"] == "BTC"
    assert 0.0 < prop["notional"] <= V1_CAPS.max_order_notional  # sized within the per-order cap
    assert prop["order"].startswith("buy ") and "BTC limit" in prop["order"]
    assert "e-" not in prop["order"].lower()  # plain decimals, re-parseable
    assert res["prediction_id"] is not None
    assert _npredictions(store) == 1


def test_small_qty_order_string_is_plain_decimal(store):
    # A tiny per-order cap forces a small qty; the order string must not use
    # scientific notation (the trade tool re-parses it).
    tight = Caps(6.0, 40.0, 100.0, ("BTC",), 90.0)
    res = propose_order("BTC", _candles(_ramp(1000, 20.0)), store, tight)
    assert res["proposal"] is not None
    assert "e-" not in res["proposal"]["order"].lower()


def test_bearish_read_proposes_sell(store):
    res = propose_order("BTC", _candles(_ramp(200, -2.0)), store, V1_CAPS)
    assert res["proposal"] is not None
    assert res["proposal"]["side"] == "sell"
    assert res["prediction_id"] is not None


def test_neutral_read_abstains_and_records_nothing(store):
    res = propose_order("BTC", _candles([100.0] * 40), store, V1_CAPS)
    assert res["proposal"] is None
    assert res["prediction_id"] is None
    assert "abstain" in res["reason"]
    assert _npredictions(store) == 0


def test_records_prediction_even_when_risk_gate_blocks_order(store):
    # Pre-load BTC exposure near the per-symbol cap so a new buy can't be funded.
    store.record_trade("paper", "BTC", "buy", 0.0006, entry_px=60000.0)  # $36 exposure
    res = propose_order("BTC", _candles(_ramp(100, 2.0)), store, V1_CAPS)
    assert res["proposal"] is None
    assert "risk gate" in res["reason"]
    assert res["prediction_id"] is not None       # forecast still logged
    assert _npredictions(store) == 1


def test_conviction_sizing_respects_floor(store):
    # A directional-but-weak lean still proposes at least the floor notional.
    res = propose_order("BTC", _candles(_ramp(100, 2.0)), store, V1_CAPS)
    assert res["proposal"]["notional"] >= MIN_PROPOSAL_NOTIONAL


def test_no_caps_abstains(store):
    res = propose_order("BTC", _candles(_ramp(100, 2.0)), store, None)
    assert res["proposal"] is None and res["prediction_id"] is None
