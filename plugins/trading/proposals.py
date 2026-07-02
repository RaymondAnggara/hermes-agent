"""Turn a market read into a PROPOSED paper order (Phase 5b build step 4b).

Bridges the advisor (4a) and the paper executor (3a): a read → a concrete,
risk-checked, conviction-sized order the operator can approve, plus a durable
**prediction** record so the eventual edge measurement (4c) has the forecast on
file. Decision support, not autopilot — this proposes and records, it does NOT
fill (the operator approves, then the `trade` tool places the paper order,
linked back to the prediction).

Honest by construction:
- **Abstain when unsure.** A neutral read (no directional lean) proposes nothing
  and records nothing — silence is a valid answer.
- **Record the forecast even when we can't act.** A directional read always logs
  its prediction (the model's call), even if the risk gate then blocks the sized
  order (e.g. per-symbol cap full) — so calibration sees every call, not just the
  ones we could fund.
- **The risk gate is still the final word.** The proposal is sized within caps
  and dry-run through :func:`plan_order`; a rejection returns no order (with the
  reason), never a fill.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from plugins.trading.advisor import market_read
from plugins.trading.marketdata import Candle
from plugins.trading.orders import RiskRejected, plan_order
from plugins.trading.risk_gate import Caps, OrderIntent
from plugins.trading.store import TradingStore

# v1 sizing constants (arbitrary starting values, reviewable — NOT learned):
#   conviction = |p − 0.5| · 2  ∈ [0, 1]; target notional scales with it, floored
#   so a proposal is meaningful and capped by the per-order cap. The directional
#   threshold mirrors the advisor's lean band (a read must clear it to act).
MIN_PROPOSAL_NOTIONAL = 5.0
DIRECTIONAL_THRESHOLD = 0.55
_QTY_DECIMALS = 6


def _floor_qty(qty: float) -> float:
    """Floor to the qty precision so the notional never rounds *up* past the
    sized target — a rounded-up sell could exceed the held position (tripping the
    no-short guard), and a rounded-up buy could breach the per-order cap."""
    scale = 10 ** _QTY_DECIMALS
    return math.floor(qty * scale) / scale


def _fmt(x: float) -> str:
    """Plain-decimal string (no scientific notation) so the order re-parses cleanly.

    A small qty like ``8.5e-05`` must render as ``0.000085`` — the ``trade`` tool
    re-parses the ``order`` string, and a relayed ``8.5e-05`` is fragile.
    """
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _conviction(p: float) -> float:
    return min(1.0, abs(p - 0.5) * 2.0)


def propose_order(
    symbol: str,
    candles: Sequence[Candle],
    store: TradingStore,
    caps: Caps | None,
    *,
    record: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read ``symbol`` and, if it leans directionally, propose a sized paper order.

    Returns a structured dict always carrying the underlying ``read``. When a
    directional order is proposed it also carries ``proposal`` (side, qty, price,
    notional, the ``order`` string to hand to the ``trade`` tool, and
    ``caps_after``) and the ``prediction_id`` it recorded. On a neutral read, or
    when the risk gate blocks the sized order, ``proposal`` is None with a
    ``reason`` (and, for a directional read, the ``prediction_id`` is still set —
    the forecast is logged regardless).
    """
    symbol = symbol.upper()
    read = market_read(symbol, candles, store, record=record, now=now)
    lean = read["lean"]
    last = read["last_price"]

    if lean == "neutral" or last is None:
        return {"proposal": None, "prediction_id": None, "reason": "no clear directional lean — abstaining", "read": read}
    if caps is None:
        return {"proposal": None, "prediction_id": None, "reason": "trading caps not configured (fail-closed)", "read": read}

    side = "buy" if lean == "bullish" else "sell"
    # Record the forecast first — a directional call is logged even if we can't
    # act on it, so calibration (4c) sees every prediction.
    prediction_id = (
        store.record_prediction(
            symbol,
            read["z"],
            read["raw_confidence_p"],
            read["confidence_p"],
            action=side,
            threshold=DIRECTIONAL_THRESHOLD,
            now=now,
        )
        if record
        else None
    )

    account = store.account_state(now=now)
    held = account.positions.get(symbol, 0.0)
    # Spot has no shorting: a bearish call is only actionable if we hold the
    # symbol (a sell trims the position). With nothing to trim, abstain — the
    # forecast is still recorded above.
    if side == "sell" and held <= 0.0:
        return {"proposal": None, "prediction_id": prediction_id, "reason": f"bearish, but no {symbol} position to trim (spot has no shorting)", "read": read}

    conviction = _conviction(read["confidence_p"])
    target_notional = min(caps.max_order_notional, max(MIN_PROPOSAL_NOTIONAL, caps.max_order_notional * conviction))
    if side == "sell":
        target_notional = min(target_notional, held)  # never propose selling more than held
    qty = _floor_qty(target_notional / last)
    if qty <= 0.0:
        return {"proposal": None, "prediction_id": prediction_id, "reason": "sized quantity rounds to zero", "read": read}

    intent = OrderIntent(side=side, symbol=symbol, qty=qty, price=last, order_type="limit")
    try:
        plan = plan_order(intent, account, caps)
    except RiskRejected as e:
        return {"proposal": None, "prediction_id": prediction_id, "reason": f"risk gate: {e.reason}", "read": read}

    return {
        "proposal": {
            "side": side,
            "symbol": symbol,
            "qty": qty,
            "price": last,
            "notional": round(qty * last, 4),
            "order": f"{side} {_fmt(qty)} {symbol} limit {_fmt(last)}",
            "caps_after": plan.caps_after,
        },
        "prediction_id": prediction_id,
        "reason": None,
        "read": read,
    }
