"""Edge measurement — scoring predictions vs buy-and-hold (Phase 5b step 4c core).

Pure functions, no I/O. This is the part that answers the only question that
matters before real money: **does acting on the signals actually beat just
holding the asset, net of fees?** (STRATEGY_DESIGN §4 — always report the
benchmark; §0 — never sell false precision.)

A prediction is scored over a fixed horizon by comparing the price when it was
made to the price ``HORIZON`` later:

- ``directional_return`` — the return from *acting* on the call (+ if a bullish
  call rose or a bearish call fell).
- ``net_return`` — that, minus round-trip fees (enter + exit).
- ``buy_and_hold_return`` — the passive benchmark over the *same* window (always
  long the asset).
- ``score_predictions`` — aggregates a set of resolved predictions into win-rate,
  mean strategy return vs mean benchmark return, and the **edge** (the
  difference). With no data it says so rather than inventing a number.

The *lookup* of the two prices (from market data) and the persistence of
outcomes are deliberately NOT here — this module is pure math so the "did it
work?" logic is trivially testable and can't lie by accident.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Round-trip is two of these. 0.1%/side ≈ Bybit spot taker; a starting estimate,
# reviewable — the honest benchmark must charge realistic fees, not zero.
FEE_RATE_PER_SIDE = 0.001
# How long after a prediction we measure its outcome. ~1 day suits the
# daily-ish trend/momentum signals; reviewable.
DEFAULT_HORIZON_HOURS = 24.0

_ACTIONS = ("buy", "sell")


def _validate(action: str, price_at: float, price_after: float) -> None:
    if action not in _ACTIONS:
        raise ValueError(f"action must be one of {_ACTIONS}, got {action!r}")
    for label, px in (("price_at", price_at), ("price_after", price_after)):
        if not math.isfinite(px) or px <= 0.0:
            raise ValueError(f"{label} must be finite and > 0, got {px!r}")


def buy_and_hold_return(price_at: float, price_after: float) -> float:
    """Passive benchmark: the return from simply holding the asset (always long)."""
    if not math.isfinite(price_at) or price_at <= 0.0:
        raise ValueError(f"price_at must be finite and > 0, got {price_at!r}")
    return (price_after - price_at) / price_at


def directional_return(action: str, price_at: float, price_after: float) -> float:
    """Return from acting on the call: + when a buy rose or a sell fell."""
    _validate(action, price_at, price_after)
    raw = (price_after - price_at) / price_at
    return raw if action == "buy" else -raw


def net_return(
    action: str,
    price_at: float,
    price_after: float,
    *,
    fee_rate: float = FEE_RATE_PER_SIDE,
) -> float:
    """Directional return minus round-trip fees (enter + exit)."""
    return directional_return(action, price_at, price_after) - 2.0 * fee_rate


def is_win(action: str, price_at: float, price_after: float) -> bool:
    """Did the call go the right way (before fees)?"""
    return directional_return(action, price_at, price_after) > 0.0


@dataclass(frozen=True)
class ScoreReport:
    """Aggregate edge report over a set of resolved predictions."""

    n: int
    wins: int
    win_rate: float | None  # fraction correct (None when n == 0)
    strategy_return: float | None  # mean per-prediction net return
    benchmark_return: float | None  # mean buy-and-hold over the same windows
    edge: float | None  # strategy_return − benchmark_return
    fee_rate_per_side: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "strategy_return": self.strategy_return,
            "benchmark_return": self.benchmark_return,
            "edge": self.edge,
            "beats_benchmark": (self.edge is not None and self.edge > 0.0),
            "fee_rate_per_side": self.fee_rate_per_side,
            "note": (
                "No resolved predictions yet — need directional calls whose "
                "horizon has elapsed before an edge can be measured."
                if self.n == 0
                else (
                    "Net of fees, over the measured windows. Compare win_rate to "
                    "0.5 (coin flip) and edge to 0 (buy-and-hold). Small n is not "
                    "evidence — treat as indicative until many samples accrue."
                )
            ),
        }


def score_predictions(
    resolved: Sequence[dict[str, Any]],
    *,
    fee_rate: float = FEE_RATE_PER_SIDE,
) -> ScoreReport:
    """Aggregate resolved predictions into a win-rate + strategy-vs-benchmark edge.

    ``resolved`` is a sequence of dicts each with ``action`` ("buy"/"sell"),
    ``price_at`` and ``price_after``. Returns a :class:`ScoreReport`; with no
    resolved predictions the rate/returns/edge are ``None`` (honest "no data"),
    never a fabricated 0.
    """
    n = len(resolved)
    if n == 0:
        return ScoreReport(0, 0, None, None, None, None, fee_rate)

    wins = 0
    strat_sum = 0.0
    bench_sum = 0.0
    for r in resolved:
        action = r["action"]
        at = float(r["price_at"])
        after = float(r["price_after"])
        _validate(action, at, after)
        if directional_return(action, at, after) > 0.0:
            wins += 1
        strat_sum += net_return(action, at, after, fee_rate=fee_rate)
        bench_sum += buy_and_hold_return(at, after)

    strategy_return = strat_sum / n
    benchmark_return = bench_sum / n
    return ScoreReport(
        n=n,
        wins=wins,
        win_rate=wins / n,
        strategy_return=strategy_return,
        benchmark_return=benchmark_return,
        edge=strategy_return - benchmark_return,
        fee_rate_per_side=fee_rate,
    )
