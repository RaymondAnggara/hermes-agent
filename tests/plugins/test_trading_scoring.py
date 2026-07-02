"""Tests for the edge-measurement scoring core (scoring.py, step 4c).

Invariants: directional return has the right sign for buy vs sell, fees make the
net return strictly worse, the benchmark is always the passive long return, and
the aggregate reports an honest "no data" at n=0 rather than a fabricated 0.
"""

from __future__ import annotations

import pytest

from plugins.trading.scoring import (
    FEE_RATE_PER_SIDE,
    buy_and_hold_return,
    directional_return,
    is_win,
    net_return,
    score_predictions,
)


# ---- per-prediction math --------------------------------------------------


def test_buy_call_that_rises_is_a_win_positive_return():
    assert directional_return("buy", 100.0, 110.0) == pytest.approx(0.10)
    assert is_win("buy", 100.0, 110.0)


def test_sell_call_that_falls_is_a_win_positive_return():
    # A bearish call profits when price drops.
    assert directional_return("sell", 100.0, 90.0) == pytest.approx(0.10)
    assert is_win("sell", 100.0, 90.0)


def test_buy_call_that_falls_loses():
    assert directional_return("buy", 100.0, 90.0) == pytest.approx(-0.10)
    assert not is_win("buy", 100.0, 90.0)


def test_fees_make_net_return_worse_by_round_trip():
    gross = directional_return("buy", 100.0, 110.0)
    net = net_return("buy", 100.0, 110.0)
    assert net == pytest.approx(gross - 2 * FEE_RATE_PER_SIDE)
    assert net < gross


def test_buy_and_hold_is_always_the_long_return():
    assert buy_and_hold_return(100.0, 110.0) == pytest.approx(0.10)
    assert buy_and_hold_return(100.0, 90.0) == pytest.approx(-0.10)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_bad_prices_raise(bad):
    with pytest.raises(ValueError):
        directional_return("buy", bad, 100.0)


def test_bad_action_raises():
    with pytest.raises(ValueError):
        directional_return("hodl", 100.0, 110.0)


# ---- aggregation ----------------------------------------------------------


def test_empty_is_honest_no_data():
    r = score_predictions([]).as_dict()
    assert r["n"] == 0
    assert r["win_rate"] is None and r["edge"] is None
    assert r["beats_benchmark"] is False
    assert "No resolved predictions" in r["note"]


def test_shorting_a_falling_market_beats_buy_and_hold():
    # Two correct bearish calls into a falling market: strategy is up, hold is down.
    resolved = [
        {"action": "sell", "price_at": 100.0, "price_after": 90.0},
        {"action": "sell", "price_at": 100.0, "price_after": 80.0},
    ]
    r = score_predictions(resolved).as_dict()
    assert r["n"] == 2 and r["wins"] == 2
    assert r["win_rate"] == pytest.approx(1.0)
    assert r["benchmark_return"] < 0.0        # buy-and-hold lost in the drop
    assert r["strategy_return"] > 0.0         # shorting the drop won (even net of fees)
    assert r["edge"] > 0.0 and r["beats_benchmark"] is True


def test_wrong_calls_lose_to_benchmark():
    # Bearish calls into a rising market: strategy loses, buy-and-hold wins.
    resolved = [
        {"action": "sell", "price_at": 100.0, "price_after": 110.0},
        {"action": "sell", "price_at": 100.0, "price_after": 120.0},
    ]
    r = score_predictions(resolved).as_dict()
    assert r["wins"] == 0 and r["win_rate"] == pytest.approx(0.0)
    assert r["benchmark_return"] > 0.0
    assert r["strategy_return"] < 0.0
    assert r["edge"] < 0.0 and r["beats_benchmark"] is False


def test_fees_can_flip_a_marginal_win_to_negative_edge():
    # A tiny correct move whose gross edge is smaller than round-trip fees.
    tiny = 2 * FEE_RATE_PER_SIDE / 2  # 0.1% move, fees are 0.2% round trip
    resolved = [{"action": "buy", "price_at": 100.0, "price_after": 100.0 * (1 + tiny)}]
    r = score_predictions(resolved).as_dict()
    assert r["wins"] == 1                      # directionally correct (pre-fee)
    assert r["strategy_return"] < r["benchmark_return"]  # but fees sink it below hold
