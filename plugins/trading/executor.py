"""Paper executor + the full paper-mode order path (TRADE_SAFETY_DESIGN §7).

Simulates fills and writes the **same** append-only audit records as a live
trade would, tagged ``paper`` — no exchange call, no key, no network. This is
order-lifecycle step 5a (PAPER fill); steps 1–4 (parse → plan → risk gate) live
in :mod:`plugins.trading.orders`.

The whole point of paper mode is that everything up to the final fill hop is
*identical* to live, so it genuinely exercises the real decision path: the same
parser, the same risk gate, the same audit store. Only the fill is simulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from plugins.trading.orders import (
    OrderParseError,
    OrderPlan,
    RiskRejected,
    parse_order,
    plan_order,
)
from plugins.trading.risk_gate import AccountState, Caps
from plugins.trading.store import TradingStore

PAPER_MODE = "paper"


@dataclass(frozen=True)
class FillResult:
    """A simulated fill written to the audit store."""

    trade_id: int
    plan_id: str
    mode: str
    symbol: str
    side: str
    qty: float
    fill_px: float
    notional: float
    created_at: str
    replayed: bool  # True if this plan was already filled (idempotent replay)


class PaperExecutor:
    """Simulates fills at the plan price and records them as ``paper`` trades.

    Idempotent on ``plan.plan_id``: executing the same plan twice returns the
    first fill instead of double-filling (the ``idempotency_key`` UNIQUE
    constraint is the durable backstop). Market orders are already priced by
    :func:`~plugins.trading.orders.plan_order`, so the plan's intent price is the
    fill price.
    """

    mode = PAPER_MODE

    def __init__(self, store: TradingStore) -> None:
        self.store = store

    def execute(
        self,
        plan: OrderPlan,
        *,
        now: datetime | None = None,
        prediction_id: int | None = None,
    ) -> FillResult:
        existing = self.store.trade_by_idempotency_key(plan.plan_id)
        if existing is not None:
            return self._result_from_row(plan, existing, replayed=True)

        intent = plan.intent
        fill_px = intent.price
        trade_id = self.store.record_trade(
            self.mode,
            intent.symbol,
            intent.side,
            intent.qty,
            entry_px=fill_px,
            outcome="open",
            prediction_id=prediction_id,
            idempotency_key=plan.plan_id,
            now=now,
        )
        return FillResult(
            trade_id=trade_id,
            plan_id=plan.plan_id,
            mode=self.mode,
            symbol=intent.symbol,
            side=intent.side,
            qty=intent.qty,
            fill_px=fill_px,
            notional=plan.est_notional,
            created_at=plan.created_at,
            replayed=False,
        )

    def _result_from_row(
        self, plan: OrderPlan, row: dict[str, Any], *, replayed: bool
    ) -> FillResult:
        qty = float(row["qty"])
        fill_px = float(row["entry_px"])
        return FillResult(
            trade_id=int(row["id"]),
            plan_id=plan.plan_id,
            mode=str(row["mode"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            qty=qty,
            fill_px=fill_px,
            notional=qty * fill_px,
            created_at=str(row["created_at"]),
            replayed=replayed,
        )


def paper_trade(
    text: str,
    store: TradingStore,
    caps: Caps | None,
    *,
    resolved_price: float | None = None,
    now: datetime | None = None,
    prediction_id: int | None = None,
) -> dict[str, Any]:
    """Run one order through the FULL paper path and return a structured result.

    parse → derive account state from the audit store → risk gate → paper fill.
    Reusable by the (later, gated) ``#trading`` ``/trade`` tool handler and fully
    testable without the gateway. Never raises for an expected rejection — a bad
    command or a gate rejection comes back as ``status="rejected"`` with the
    reason, so the caller can reply + audit and stop. Only the fill mutates state.
    """
    try:
        intent = parse_order(text)
    except OrderParseError as e:
        return {"ok": False, "status": "rejected", "stage": "parse", "reason": str(e)}

    account: AccountState = store.account_state(now=now)
    try:
        plan = plan_order(intent, account, caps, resolved_price=resolved_price)
    except RiskRejected as e:
        return {"ok": False, "status": "rejected", "stage": "risk_gate", "reason": e.reason}

    fill = PaperExecutor(store).execute(plan, now=now, prediction_id=prediction_id)
    return {
        "ok": True,
        "status": "filled",
        "mode": fill.mode,
        "plan_id": fill.plan_id,
        "trade_id": fill.trade_id,
        "prediction_id": prediction_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "qty": fill.qty,
        "fill_px": fill.fill_px,
        "notional": fill.notional,
        "caps_after": plan.caps_after,
        "replayed": fill.replayed,
    }
