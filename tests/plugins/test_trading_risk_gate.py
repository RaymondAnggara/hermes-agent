"""Adversarial tests for the fail-closed risk gate (TRADE_SAFETY_DESIGN §4).

Every rejection path must fire, the unconfigured/garbage cases must fail closed,
and a deliberately too-loose config must still be reined in by HARD_CEILING.
"""

from __future__ import annotations

import math

import pytest

from plugins.trading.risk_gate import (
    HARD_CEILING,
    AccountState,
    Caps,
    OrderIntent,
    clamp_caps,
    est_notional,
    risk_gate,
)

# Confirmed v1 caps ($100 Bybit spot, 2026-06-29).
V1_CAPS = Caps(
    max_order_notional=20.0,
    max_position_per_symbol=40.0,
    max_period_notional=100.0,
    allowed_symbols=("BTC", "ETH"),
    hard_ceiling=90.0,
)

EMPTY = AccountState()


def _order(symbol="BTC", qty=1.0, price=10.0, side="buy") -> OrderIntent:
    return OrderIntent(side=side, symbol=symbol, qty=qty, price=price)


def test_accepts_valid_in_cap_order_with_headroom():
    d = risk_gate(_order(qty=1.0, price=10.0), EMPTY, V1_CAPS)  # notional 10
    assert d.accepted
    assert d.caps_after is not None
    assert d.caps_after["order_headroom"] == pytest.approx(10.0)  # 20 - 10
    assert d.caps_after["total_remaining"] == pytest.approx(80.0)  # 90 - 10
    assert all(v >= 0 for v in d.caps_after.values())


def test_unconfigured_caps_reject_everything():
    assert not risk_gate(_order(), EMPTY, None).accepted


@pytest.mark.parametrize(
    "bad",
    [
        Caps(0.0, 40.0, 100.0, ("BTC",), 90.0),  # max_order_notional
        Caps(20.0, 0.0, 100.0, ("BTC",), 90.0),  # max_position_per_symbol
        Caps(20.0, 40.0, 0.0, ("BTC",), 90.0),  # max_period_notional
        Caps(20.0, 40.0, 100.0, ("BTC",), 0.0),  # hard_ceiling
    ],
    ids=["max_order_notional", "max_position_per_symbol", "max_period_notional", "hard_ceiling"],
)
def test_any_non_positive_cap_fails_closed(bad):
    assert not risk_gate(_order(price=1.0), EMPTY, bad).accepted


def test_empty_allowlist_rejects():
    caps = Caps(20.0, 40.0, 100.0, (), 90.0)
    assert not risk_gate(_order(), EMPTY, caps).accepted


def test_symbol_not_in_allowlist_rejected():
    d = risk_gate(_order(symbol="DOGE"), EMPTY, V1_CAPS)
    assert not d.accepted
    assert "allowlist" in d.reason


def test_over_per_order_notional_rejected():
    # notional 25 > max_order_notional 20
    d = risk_gate(_order(qty=2.5, price=10.0), EMPTY, V1_CAPS)
    assert not d.accepted
    assert "max_order_notional" in d.reason


def test_over_period_notional_cap_exhaustion_rejected():
    # 95 already traded in 24h + 10 = 105 > max_period_notional 100
    acct = AccountState(positions={}, rolling_24h_notional=95.0)
    d = risk_gate(_order(qty=1.0, price=10.0), acct, V1_CAPS)
    assert not d.accepted
    assert "max_period_notional" in d.reason


def test_over_per_symbol_position_rejected():
    # already 35 in BTC + 10 = 45 > max_position_per_symbol 40 (order itself is in-cap)
    acct = AccountState(positions={"BTC": 35.0}, rolling_24h_notional=0.0)
    d = risk_gate(_order(qty=1.0, price=10.0), acct, V1_CAPS)
    assert not d.accepted
    assert "max_position_per_symbol" in d.reason


def test_total_exposure_ceiling_fires_before_other_caps_pass():
    """Construct a case where per-order, per-symbol, and period all pass but the
    total-exposure backstop is the binding constraint."""
    # Three symbols would be needed to keep each under per-symbol while total breaches;
    # v1 allows BTC,ETH only. Use a looser per-symbol so only the ceiling binds.
    caps = Caps(
        max_order_notional=20.0,
        max_position_per_symbol=80.0,  # generous → won't bind
        max_period_notional=1000.0,  # generous → won't bind
        allowed_symbols=("BTC", "ETH"),
        hard_ceiling=90.0,
    )
    acct = AccountState(positions={"BTC": 40.0, "ETH": 40.0}, rolling_24h_notional=0.0)  # 80 open
    d = risk_gate(_order(symbol="BTC", qty=1.0, price=15.0), acct, caps)  # +15 → total 95 > 90
    assert not d.accepted
    assert "HARD_CEILING" in d.reason or "total exposure" in d.reason


@pytest.mark.parametrize("qty", [0.0, -1.0, math.inf, math.nan])
def test_bad_qty_rejected(qty):
    assert not risk_gate(_order(qty=qty), EMPTY, V1_CAPS).accepted


@pytest.mark.parametrize("price", [0.0, -5.0, math.inf, math.nan])
def test_bad_price_rejected(price):
    assert not risk_gate(_order(price=price), EMPTY, V1_CAPS).accepted


def test_bad_side_rejected():
    assert not risk_gate(_order(side="hodl"), EMPTY, V1_CAPS).accepted


def test_empty_symbol_rejected():
    assert not risk_gate(_order(symbol=""), EMPTY, V1_CAPS).accepted


def test_corrupt_account_state_fails_closed():
    bad_roll = AccountState(positions={}, rolling_24h_notional=-1.0)
    assert not risk_gate(_order(), bad_roll, V1_CAPS).accepted
    bad_pos = AccountState(positions={"BTC": math.nan}, rolling_24h_notional=0.0)
    assert not risk_gate(_order(), bad_pos, V1_CAPS).accepted


def test_hard_ceiling_clamps_a_too_loose_config():
    """Config can only tighten. A wildly loose config is clamped down to the
    absolute HARD_CEILING, and the gate then enforces the clamped value."""
    loose = Caps(
        max_order_notional=10_000.0,
        max_position_per_symbol=10_000.0,
        max_period_notional=10_000.0,
        allowed_symbols=("BTC", "ETH"),
        hard_ceiling=10_000.0,  # tries to raise the absolute ceiling
    )
    clamped = clamp_caps(loose)
    assert clamped.max_order_notional == HARD_CEILING
    assert clamped.max_position_per_symbol == HARD_CEILING
    assert clamped.hard_ceiling == HARD_CEILING
    # An order that the loose config would wave through is still capped: a single
    # order of notional 95 exceeds the (clamped) per-order cap of 90.
    d = risk_gate(_order(qty=95.0, price=1.0), EMPTY, loose)
    assert not d.accepted


def test_clamp_caps_never_returns_exposure_cap_above_ceiling():
    """Invariant: no clamped exposure cap exceeds HARD_CEILING, for any input."""
    for value in (0.5, 50.0, 89.9, 90.0, 90.1, 1e9):
        caps = Caps(value, value, value, ("BTC",), value)
        c = clamp_caps(caps)
        assert c.max_order_notional <= HARD_CEILING
        assert c.max_position_per_symbol <= HARD_CEILING
        assert c.hard_ceiling <= HARD_CEILING


def test_period_notional_is_turnover_not_clamped_to_ceiling():
    """max_period_notional is turnover (round-trips), intentionally allowed
    above HARD_CEILING — the v1 config has 100 > 90 on purpose."""
    assert clamp_caps(V1_CAPS).max_period_notional == 100.0


def test_est_notional():
    assert est_notional(_order(qty=2.0, price=3.0)) == 6.0
