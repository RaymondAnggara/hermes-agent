"""Resolve recorded predictions against later prices + assemble the edge report.

Pure (candles are passed in, no fetch here). Turns the forecast log + a price
series into the ``{action, price_at, price_after}`` rows :mod:`scoring` scores.

A prediction is resolvable only once its horizon has elapsed AND both the
decision-time and horizon-later prices fall within the available history;
otherwise it is *pending* (counted, never scored). Prices are looked up from the
candle series (nearest candle at-or-before the timestamp) — a documented
approximation: the exact decision-time price is not stored, and CoinGecko's free
history is ~30 days, so predictions older than that can't be resolved.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.trading.marketdata import Candle
from plugins.trading.scoring import (
    DEFAULT_HORIZON_HOURS,
    FEE_RATE_PER_SIDE,
    score_predictions,
)


def price_at_or_before(candles: Sequence[Candle], ts_ms: int) -> float | None:
    """Close of the latest candle at or before ``ts_ms`` (candles ascending)."""
    best: Candle | None = None
    for c in candles:
        if c.ts_ms <= ts_ms:
            best = c
        else:
            break
    return best.close if best is not None else None


def resolve_predictions(
    predictions: Sequence[dict[str, Any]],
    candles: Sequence[Candle],
    *,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(resolved, pending_count)`` for a symbol's predictions.

    ``resolved`` items carry ``action``/``price_at``/``price_after`` (+ ``raw_p``)
    ready for :func:`~plugins.trading.scoring.score_predictions`. A prediction is
    pending if its horizon hasn't elapsed or a needed price is outside history.
    """
    now = now or datetime.now(timezone.utc)
    horizon = timedelta(hours=horizon_hours)
    resolved: list[dict[str, Any]] = []
    pending = 0
    for p in predictions:
        decided = datetime.fromisoformat(p["decided_at"])
        after_time = decided + horizon
        if after_time > now:
            pending += 1  # horizon not elapsed yet
            continue
        price_at = price_at_or_before(candles, int(decided.timestamp() * 1000))
        price_after = price_at_or_before(candles, int(after_time.timestamp() * 1000))
        if price_at is None or price_after is None:
            pending += 1  # outside available price history
            continue
        resolved.append(
            {
                "action": p["action"],
                "price_at": price_at,
                "price_after": price_after,
                "raw_p": p.get("raw_p"),
            }
        )
    return resolved, pending


def strategy_report(
    store: Any,
    candles_by_symbol: dict[str, Sequence[Candle]],
    symbols: Sequence[str],
    *,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    fee_rate: float = FEE_RATE_PER_SIDE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve each symbol's forecast log and score it vs buy-and-hold.

    Returns per-symbol reports + an overall aggregate, each carrying ``pending``
    (predictions whose horizon hasn't elapsed). Honest at n=0 via the underlying
    :class:`~plugins.trading.scoring.ScoreReport`.
    """
    per_symbol: dict[str, Any] = {}
    all_resolved: list[dict[str, Any]] = []
    total_pending = 0
    for sym in symbols:
        sym = sym.upper()
        preds = store.list_predictions(sym)
        candles = candles_by_symbol.get(sym) or []
        resolved, pending = resolve_predictions(
            preds, candles, horizon_hours=horizon_hours, now=now
        )
        rep = score_predictions(resolved, fee_rate=fee_rate).as_dict()
        rep["symbol"] = sym
        rep["pending"] = pending
        per_symbol[sym] = rep
        all_resolved.extend(resolved)
        total_pending += pending

    overall = score_predictions(all_resolved, fee_rate=fee_rate).as_dict()
    overall["pending"] = total_pending
    return {
        "horizon_hours": horizon_hours,
        "fee_rate_per_side": fee_rate,
        "overall": overall,
        "per_symbol": per_symbol,
    }
