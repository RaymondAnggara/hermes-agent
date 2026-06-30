"""Confidence model — a "panel of advisors" (signed signals + logistic).

Pure functions, no I/O. Implements STRATEGY_DESIGN.md §2:

    Step 1 — overall lean:   z = b + Σ (wᵢ · sᵢ)
    Step 2 — to a percent:   p = 1 / (1 + e^−z)     (the sigmoid)

Each signal ``sᵢ ∈ [−1, +1]`` is one advisor's opinion (−1 strong sell, 0
no-opinion/unknown, +1 strong buy); each weight ``wᵢ ≥ 0`` is how much that
advisor has earned our trust. Two properties the design depends on:

- **Adding (not multiplying)** — an advisor who says "I don't know" (``s=0``)
  contributes exactly 0: no opinion, no penalty. Bullish advisors reinforce.
- **Direction lives in the sign of ``z``** — negative leans sell, 0 is a
  coin-flip, positive leans buy. The sigmoid squashes any ``z`` into 0–100%.

Security note (STRATEGY_DESIGN §2): the confidence number is **never** settable
by free text. Prose is converted to *structured* :class:`Signal` values by a
constrained extraction step (built later); this module only does the math. To
keep that boundary safe, this module also **fails closed on garbage**: a
non-finite or negatively-weighted signal raises rather than silently producing
a bogus confidence, and an out-of-range opinion is clamped into ``[−1, +1]`` so
an extraction bug cannot blow ``z`` up.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    """One advisor's opinion about a symbol.

    ``s`` is the opinion in ``[−1, +1]`` (clamped on use); ``w`` is the trust
    weight, ``≥ 0``. ``name`` is a human label (e.g. ``"market_data"``) used in
    error messages and audit records only — it does not affect the math.
    """

    name: str
    s: float
    w: float


def sigmoid(z: float) -> float:
    """Numerically stable logistic ``1 / (1 + e^−z)`` → ``(0, 1)``."""
    if not math.isfinite(z):
        raise ValueError(f"z must be finite, got {z!r}")
    # Split on the sign of z so we never call exp() on a large positive number
    # (which would overflow); both branches are algebraically identical.
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def confidence_z(signals: Iterable[Signal], bias: float = 0.0) -> float:
    """Weighted lean ``z = bias + Σ (wᵢ · clamp(sᵢ))``.

    Fail-closed: raises ``ValueError`` on a non-finite bias, or any signal with
    a non-finite ``s``/``w`` or a negative weight (weights are learned
    internally, so a negative one is a bug, not adversarial input). The opinion
    ``s`` is clamped into ``[−1, +1]`` — an unknown advisor (``s=0``) therefore
    contributes exactly 0, never a penalty.
    """
    if not math.isfinite(bias):
        raise ValueError(f"bias must be finite, got {bias!r}")
    z = bias
    for sig in signals:
        if not math.isfinite(sig.s):
            raise ValueError(f"signal {sig.name!r} has non-finite opinion {sig.s!r}")
        if not math.isfinite(sig.w):
            raise ValueError(f"signal {sig.name!r} has non-finite weight {sig.w!r}")
        if sig.w < 0.0:
            raise ValueError(
                f"signal {sig.name!r} has negative weight {sig.w!r}; weights must be ≥ 0"
            )
        s = max(-1.0, min(1.0, sig.s))  # clamp opinion into [−1, +1]
        z += sig.w * s
    return z


def confidence_p(signals: Iterable[Signal], bias: float = 0.0) -> float:
    """Confidence as a probability in ``(0, 1)`` — ``sigmoid(confidence_z(...))``."""
    return sigmoid(confidence_z(signals, bias))


@dataclass(frozen=True)
class CalibrationBand:
    """A confidence band and the win-rate actually realized inside it.

    ``lo``/``hi`` bound the *raw* confidence (``lo`` inclusive, ``hi``
    exclusive, except the top band whose ``hi`` is inclusive at 1.0).
    ``realized`` is the fraction of past predictions in this band that actually
    won. Bands are expected non-overlapping and contiguous.
    """

    lo: float
    hi: float
    realized: float


def calibrate(raw_p: float, bands: Sequence[CalibrationBand] = ()) -> float:
    """Remap a raw confidence to the realized win-rate of its band (stub).

    STRATEGY_DESIGN §5(B): a "70%" that historically only won 58% should be
    reported as 58%, per band, learned from data. This is the **stub** for that
    remap: with no bands yet (a fresh, untrained system) it returns ``raw_p``
    unchanged — an uncalibrated model honestly reports its raw number rather
    than inventing a correction. Once bands exist it returns the realized rate
    of the band containing ``raw_p``.

    Fail-closed on an out-of-range input; if no band matches (a gap in the band
    coverage) the raw value passes through unchanged rather than guessing.
    """
    if not math.isfinite(raw_p) or not (0.0 <= raw_p <= 1.0):
        raise ValueError(f"raw_p must be a probability in [0, 1], got {raw_p!r}")
    for band in bands:
        in_band = band.lo <= raw_p < band.hi or (raw_p == band.hi == 1.0)
        if in_band:
            return band.realized
    return raw_p
