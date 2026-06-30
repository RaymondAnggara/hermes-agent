"""Order parsing + planning (TRADE_SAFETY_DESIGN §3 / §9 step 2).

Pure logic, no I/O. Turns operator free-text into a structured
:class:`~plugins.trading.risk_gate.OrderIntent`, then runs the fail-closed risk
gate to produce an :class:`OrderPlan`. This is steps 1–4 of the order lifecycle
(PARSE → RESOLVE → risk_gate → PLAN); the simulated/live fill (step 5) is the
executor's job, built later.

Fail-closed throughout: a malformed command raises :class:`OrderParseError`, and
anything the gate rejects (or a market order with no resolved price) raises
:class:`RiskRejected` carrying the reason — the caller audits the rejection and
stops, never falling through to an order.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from plugins.trading.risk_gate import (
    AccountState,
    Caps,
    OrderIntent,
    est_notional,
    risk_gate,
)

_SIDES = ("buy", "sell")
_ORDER_TYPES = ("market", "limit")


class OrderParseError(ValueError):
    """The order text could not be parsed into a valid intent."""


class RiskRejected(Exception):
    """The order did not pass the risk gate (or a precondition for it).

    ``reason`` is a human-readable explanation suitable for replying to the
    operator and writing to the audit log.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OrderPlan:
    """A risk-approved plan, not yet executed. ``intent`` is the *priced* intent
    (market price resolved). ``caps_after`` is the headroom remaining if this
    order fills."""

    plan_id: str
    intent: OrderIntent
    est_notional: float
    caps_after: dict[str, float]
    created_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_order(args: str) -> OrderIntent:
    """Parse ``<side> <qty> <symbol> <type> [<price>]`` into an OrderIntent.

    Examples: ``buy 0.01 BTC limit 60000`` or ``sell 0.5 ETH market``. The
    leading ``/trade`` (if any) must already be stripped. A limit order requires
    a positive price; a market order takes no price (it is resolved at plan
    time, see :func:`plan_order`). Raises :class:`OrderParseError` on anything
    malformed — there is no lenient fallback.
    """
    tokens = args.split()
    if len(tokens) < 4:
        raise OrderParseError(
            "expected '<side> <qty> <symbol> <type> [<price>]', "
            f"e.g. 'buy 0.01 BTC limit 60000' (got {args!r})"
        )

    side = tokens[0].lower()
    if side not in _SIDES:
        raise OrderParseError(f"side must be one of {_SIDES}, got {tokens[0]!r}")

    try:
        qty = float(tokens[1])
    except ValueError:
        raise OrderParseError(f"qty must be a number, got {tokens[1]!r}") from None
    if not (qty > 0.0) or qty != qty or qty in (float("inf"), float("-inf")):
        raise OrderParseError(f"qty must be finite and > 0, got {tokens[1]!r}")

    symbol = tokens[2].upper()
    if not symbol.isalnum():
        raise OrderParseError(f"symbol must be alphanumeric, got {tokens[2]!r}")

    order_type = tokens[3].lower()
    if order_type not in _ORDER_TYPES:
        raise OrderParseError(f"type must be one of {_ORDER_TYPES}, got {tokens[3]!r}")

    if order_type == "limit":
        if len(tokens) != 5:
            raise OrderParseError("a limit order requires exactly one price token")
        try:
            price = float(tokens[4])
        except ValueError:
            raise OrderParseError(f"price must be a number, got {tokens[4]!r}") from None
        if not (price > 0.0) or price != price or price in (float("inf"), float("-inf")):
            raise OrderParseError(f"price must be finite and > 0, got {tokens[4]!r}")
    else:  # market
        if len(tokens) != 4:
            raise OrderParseError("a market order takes no price (resolved at plan time)")
        price = 0.0  # resolved in plan_order via resolved_price

    return OrderIntent(side=side, symbol=symbol, qty=qty, price=price, order_type=order_type)


def plan_order(
    intent: OrderIntent,
    account: AccountState,
    caps: Caps | None,
    *,
    resolved_price: float | None = None,
) -> OrderPlan:
    """Run the risk gate and build an :class:`OrderPlan` (lifecycle steps 2–4).

    For a market order, ``resolved_price`` (from read-only market data) must be
    supplied and positive; for a limit order the intent's own price is used.
    Raises :class:`RiskRejected` if the price cannot be resolved or the gate
    rejects — fail-closed, never returns an unapproved plan.
    """
    if intent.order_type == "market":
        if resolved_price is None or not (resolved_price > 0.0):
            raise RiskRejected("market order requires a positive resolved price")
        priced = replace(intent, price=resolved_price)
    else:
        priced = intent

    decision = risk_gate(priced, account, caps)
    if not decision.accepted or decision.caps_after is None:
        raise RiskRejected(decision.reason)

    return OrderPlan(
        plan_id=uuid.uuid4().hex[:12],
        intent=priced,
        est_notional=est_notional(priced),
        caps_after=decision.caps_after,
        created_at=_utc_now_iso(),
    )
