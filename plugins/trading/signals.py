"""Pure market-data signals (Phase 5b build step 4a, STRATEGY_DESIGN §3).

Turn a series of OHLC candles into signed opinions ``sᵢ ∈ [−1, +1]`` for the
confidence model. Pure functions, no I/O.

Fail-closed distinction: a signal that lacks enough data to compute returns
``None`` (unavailable) rather than a fake neutral ``0`` — "I couldn't look" is
NOT the same as "I looked and it's neutral". The advisor reports which signals
fired vs which were unavailable, so a confidence number is never quietly built
on absent inputs.

v1 catalog (technical, derived from market data — the §3 "market data" advisor
split into two directional reads):

- ``trend``     — fast SMA vs slow SMA, relative gap → lean
- ``momentum``  — recent N-candle return → lean

Starting weights are HAND-SET defaults, deliberately modest — they are NOT yet
learned or calibrated against outcomes (that is build step 4c), so an
uncalibrated model must not shout.
"""

from __future__ import annotations

from collections.abc import Sequence

from plugins.trading.confidence import Signal
from plugins.trading.marketdata import Candle

# Hand-set starting weights (NOT learned yet — 4c replaces these from data).
TREND_WEIGHT = 1.0
MOMENTUM_WEIGHT = 0.6

# Windows, in candles. At CoinGecko's ~4h granularity (days=30): fast≈1d,
# slow≈5d, momentum lookback≈2d.
_TREND_FAST = 6
_TREND_SLOW = 30
_MOMENTUM_LOOKBACK = 12

# Full-scale reference moves: a gap/return this large maps to a ±1 opinion.
_TREND_FULLSCALE = 0.05  # 5% fast-vs-slow gap
_MOMENTUM_FULLSCALE = 0.10  # 10% move over the lookback window


def _closes(candles: Sequence[Candle]) -> list[float]:
    return [c.close for c in candles]


def _sma(values: Sequence[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def trend_signal(
    candles: Sequence[Candle],
    *,
    fast: int = _TREND_FAST,
    slow: int = _TREND_SLOW,
    weight: float = TREND_WEIGHT,
) -> Signal | None:
    """Fast-vs-slow SMA gap → signed trend opinion, or None if too little data."""
    closes = _closes(candles)
    fma = _sma(closes, fast)
    sma = _sma(closes, slow)
    if fma is None or sma is None or sma <= 0.0:
        return None
    rel = (fma - sma) / sma  # +ve when the fast average is above the slow one
    return Signal(name="trend", s=_clip(rel / _TREND_FULLSCALE), w=weight)


def momentum_signal(
    candles: Sequence[Candle],
    *,
    lookback: int = _MOMENTUM_LOOKBACK,
    weight: float = MOMENTUM_WEIGHT,
) -> Signal | None:
    """Recent N-candle return → signed momentum opinion, or None if too little data."""
    closes = _closes(candles)
    if len(closes) < lookback + 1:
        return None
    past = closes[-(lookback + 1)]
    if past <= 0.0:
        return None
    ret = (closes[-1] - past) / past
    return Signal(name="momentum", s=_clip(ret / _MOMENTUM_FULLSCALE), w=weight)


def compute_signals(candles: Sequence[Candle]) -> tuple[list[Signal], list[str]]:
    """Compute all v1 signals.

    Returns ``(available, unavailable)``: the Signal objects that could be
    computed, and the names of those that could not (insufficient data).
    """
    signals: list[Signal] = []
    unavailable: list[str] = []
    for name, fn in (("trend", trend_signal), ("momentum", momentum_signal)):
        sig = fn(candles)
        if sig is None:
            unavailable.append(name)
        else:
            signals.append(sig)
    return signals, unavailable
