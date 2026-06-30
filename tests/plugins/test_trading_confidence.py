"""Adversarial tests for the trading confidence model (STRATEGY_DESIGN §2).

Invariants, not snapshots: we assert the *properties* the design depends on
(direction, unknown-signal neutrality, additivity, fail-closed) rather than
frozen numbers.
"""

from __future__ import annotations

import math

import pytest

from plugins.trading.confidence import (
    CalibrationBand,
    Signal,
    calibrate,
    confidence_p,
    confidence_z,
    sigmoid,
)


def test_sigmoid_midpoint_bounds_and_monotonic():
    assert sigmoid(0.0) == pytest.approx(0.5)
    # Strict monotonicity in a range float64 can still resolve.
    assert 0.0 < sigmoid(-5.0) < sigmoid(0.0) < sigmoid(5.0) < 1.0
    # Stable at extremes — must not overflow (saturates to the bounds).
    assert sigmoid(1000.0) == pytest.approx(1.0)
    assert sigmoid(-1000.0) == pytest.approx(0.0)


def test_sigmoid_rejects_non_finite():
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError):
            sigmoid(bad)


def test_unknown_signal_has_no_effect_not_a_penalty():
    """An advisor that says 'I don't know' (s=0) contributes exactly 0 —
    additivity, not multiplication. Adding it must not change the result."""
    base = [Signal("economy", 0.3, 0.5), Signal("market", 0.2, 1.0)]
    with_unknown = base + [Signal("social", 0.0, 0.3)]
    assert confidence_z(with_unknown) == confidence_z(base)
    assert confidence_p(with_unknown) == confidence_p(base)
    # And it is NOT a penalty: still a (mild) buy, not dragged toward 0.5+below.
    assert confidence_p(with_unknown) > 0.5


def test_direction_trusted_bearish_flips_below_half():
    """A trusted bearish advisor must outweigh weak bullish ones → p < 0.5."""
    bullish = [Signal("economy", 0.3, 0.5), Signal("social", 0.6, 0.3)]
    assert confidence_p(bullish) > 0.5  # mild buy without market data
    with_bearish_market = bullish + [Signal("market", -0.7, 1.0)]
    assert confidence_p(with_bearish_market) < 0.5  # trusted bearish flips it


def test_adding_bullish_advisors_reinforces():
    one = [Signal("a", 0.5, 1.0)]
    two = [Signal("a", 0.5, 1.0), Signal("b", 0.5, 1.0)]
    assert confidence_p(two) > confidence_p(one) > 0.5


def test_opinion_clamped_so_extraction_bug_cannot_blow_up_z():
    """An out-of-range opinion is clamped to [-1, 1]; s=5 behaves like s=1."""
    assert confidence_z([Signal("x", 5.0, 1.0)]) == confidence_z([Signal("x", 1.0, 1.0)])
    assert confidence_z([Signal("x", -9.0, 2.0)]) == confidence_z([Signal("x", -1.0, 2.0)])


def test_higher_weight_means_more_influence():
    weak = confidence_z([Signal("m", 1.0, 0.2)])
    strong = confidence_z([Signal("m", 1.0, 2.0)])
    assert strong > weak > 0.0


def test_bias_shifts_the_lean():
    assert confidence_z([], bias=0.0) == 0.0
    assert confidence_p([], bias=0.0) == pytest.approx(0.5)
    assert confidence_p([], bias=1.0) > 0.5
    assert confidence_p([], bias=-1.0) < 0.5


def test_fail_closed_on_negative_weight():
    with pytest.raises(ValueError):
        confidence_z([Signal("m", 0.5, -0.1)])


def test_fail_closed_on_non_finite_signal_or_bias():
    for bad in (math.inf, math.nan):
        with pytest.raises(ValueError):
            confidence_z([Signal("m", bad, 1.0)])
        with pytest.raises(ValueError):
            confidence_z([Signal("m", 0.5, bad)])
        with pytest.raises(ValueError):
            confidence_z([], bias=bad)


def test_calibrate_identity_when_untrained():
    """No bands yet → report the raw number honestly, no invented correction."""
    assert calibrate(0.73) == 0.73
    assert calibrate(0.0) == 0.0
    assert calibrate(1.0) == 1.0


def test_calibrate_remaps_to_realized_winrate():
    bands = [
        CalibrationBand(0.0, 0.5, 0.30),
        CalibrationBand(0.5, 1.0, 0.58),  # top band: hi inclusive at 1.0
    ]
    assert calibrate(0.73, bands) == 0.58  # "73%" really won 58%
    assert calibrate(0.20, bands) == 0.30
    assert calibrate(1.0, bands) == 0.58  # boundary at 1.0 hits the top band
    assert calibrate(0.5, bands) == 0.58  # lo inclusive


def test_calibrate_passes_through_on_band_gap():
    bands = [CalibrationBand(0.0, 0.4, 0.25)]  # nothing covers (0.4, 1.0]
    assert calibrate(0.9, bands) == 0.9


def test_calibrate_rejects_out_of_range():
    for bad in (-0.01, 1.01, math.inf, math.nan):
        with pytest.raises(ValueError):
            calibrate(bad)
