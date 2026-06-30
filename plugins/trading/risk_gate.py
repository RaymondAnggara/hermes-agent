"""The in-code, fail-closed risk gate — the core of safety invariant I2.

Pure functions, no I/O (account state is passed in; the caller reads it from the
append-only audit store). Implements TRADE_SAFETY_DESIGN.md §4. Every order MUST
pass :func:`risk_gate` before it can reach an exchange adapter.

Two safety properties this module guarantees in code, not config:

1. **Unconfigured = closed.** Missing/zero caps reject everything. Nothing
   trades until caps are positively configured.
2. **Config can only tighten, never loosen.** :data:`HARD_CEILING` is an
   absolute constant; :func:`clamp_caps` clamps every exposure cap down to it,
   and the gate's total-exposure check uses the clamped ceiling. No single
   config edit or bug can deploy more than ``HARD_CEILING`` of total open
   exposure across all symbols.

Confirmed v1 caps ($100 Bybit spot account — 2026-06-29): ``max_order_notional
20``, ``max_position_per_symbol 40``, ``max_period_notional 100`` (rolling 24h),
``allowed_symbols ["BTC", "ETH"]``, ``HARD_CEILING 90`` (total exposure).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

# Absolute backstop on TOTAL open exposure (sum of all open positions across
# every symbol), enforced in code. Config caps are clamped DOWN to this — config
# can make the system tighter, never looser. Bumping real exposure past this
# requires editing this constant (a code change, reviewed), not just config.
HARD_CEILING: float = 90.0

_SIDES = ("buy", "sell")


@dataclass(frozen=True)
class OrderIntent:
    """A structured, parsed order. ``price`` is an estimated unit price used to
    size the order's notional (``qty * price``); upstream resolves it from
    read-only market data."""

    side: str
    symbol: str
    qty: float
    price: float


@dataclass(frozen=True)
class Caps:
    """Risk caps (from ``config.yaml`` ``trading.caps``, then :func:`clamp_caps`).

    ``allowed_symbols`` is an explicit allowlist — there is no "trade anything".
    """

    max_order_notional: float
    max_position_per_symbol: float
    max_period_notional: float  # rolling 24h cumulative traded notional (turnover)
    allowed_symbols: tuple[str, ...]
    hard_ceiling: float = HARD_CEILING


@dataclass(frozen=True)
class AccountState:
    """Durable account state, read from the audit store by the caller.

    ``positions`` maps symbol → current open position notional. ``rolling_24h_notional``
    is the cumulative traded notional (turnover) over the trailing 24h.
    """

    positions: Mapping[str, float] = field(default_factory=dict)
    rolling_24h_notional: float = 0.0


@dataclass(frozen=True)
class Decision:
    """Result of the gate. ``caps_after`` (only on accept) reports the remaining
    headroom for the order plan; on reject it is ``None`` and ``reason`` says
    which check fired."""

    accepted: bool
    reason: str
    caps_after: dict[str, float] | None = None


def est_notional(intent: OrderIntent) -> float:
    """Estimated notional of an order = ``qty * price``."""
    return intent.qty * intent.price


def clamp_caps(caps: Caps) -> Caps:
    """Clamp exposure caps down to the absolute :data:`HARD_CEILING`.

    ``max_order_notional`` and ``max_position_per_symbol`` are exposure-like and
    can never legitimately exceed the total-exposure ceiling, so they are
    clamped, as is ``hard_ceiling`` itself. ``max_period_notional`` is a
    *turnover* measure (cumulative 24h traded notional, which may exceed
    instantaneous exposure via round-trips — the confirmed v1 config has period
    100 > ceiling 90 on purpose), so it is intentionally NOT clamped here; the
    total-exposure backstop is enforced separately in :func:`risk_gate`.
    """
    ceiling = min(caps.hard_ceiling, HARD_CEILING)
    return Caps(
        max_order_notional=min(caps.max_order_notional, ceiling),
        max_position_per_symbol=min(caps.max_position_per_symbol, ceiling),
        max_period_notional=caps.max_period_notional,
        allowed_symbols=caps.allowed_symbols,
        hard_ceiling=ceiling,
    )


def _reject(reason: str) -> Decision:
    return Decision(accepted=False, reason=reason, caps_after=None)


def risk_gate(
    intent: OrderIntent,
    account: AccountState,
    caps: Caps | None,
) -> Decision:
    """Fail-closed risk gate (TRADE_SAFETY_DESIGN §4). Pure, no I/O.

    Rejects (in order) on: unconfigured/zero caps, an unparseable/non-finite/
    non-positive intent, a symbol outside the allowlist, over per-order notional,
    over rolling-24h period notional, over per-symbol position, and over the
    total-exposure ``HARD_CEILING``. Otherwise accepts and reports remaining
    headroom. Exposure is projected conservatively (every order is treated as
    adding its notional to exposure), so the gate can only ever be *stricter*
    than reality — the safe direction for a backstop.
    """
    # 1. Unconfigured = closed. Any missing or non-positive cap rejects all.
    if caps is None:
        return _reject("caps unconfigured (None) — fail closed")
    caps = clamp_caps(caps)
    for name, value in (
        ("max_order_notional", caps.max_order_notional),
        ("max_position_per_symbol", caps.max_position_per_symbol),
        ("max_period_notional", caps.max_period_notional),
        ("hard_ceiling", caps.hard_ceiling),
    ):
        if not math.isfinite(value) or value <= 0.0:
            return _reject(f"cap {name}={value!r} not positive — fail closed")
    if not caps.allowed_symbols:
        return _reject("allowed_symbols empty — nothing is tradeable")

    # 2. Invalid intent → fail closed (before any arithmetic on it).
    if intent.side not in _SIDES:
        return _reject(f"invalid side {intent.side!r}")
    if not intent.symbol:
        return _reject("empty symbol")
    if not math.isfinite(intent.qty) or intent.qty <= 0.0:
        return _reject(f"invalid qty {intent.qty!r} (must be finite and > 0)")
    if not math.isfinite(intent.price) or intent.price <= 0.0:
        return _reject(f"invalid price {intent.price!r} (must be finite and > 0)")

    notional = est_notional(intent)
    if not math.isfinite(notional) or notional <= 0.0:
        return _reject(f"invalid notional {notional!r}")

    # 3. Symbol allowlist.
    if intent.symbol not in caps.allowed_symbols:
        return _reject(f"symbol {intent.symbol!r} not in allowlist {caps.allowed_symbols!r}")

    # Read account state (conservatively; unknown symbol → 0 position).
    if not math.isfinite(account.rolling_24h_notional) or account.rolling_24h_notional < 0.0:
        return _reject(f"invalid rolling_24h_notional {account.rolling_24h_notional!r}")
    current_symbol_pos = account.positions.get(intent.symbol, 0.0)
    total_open = sum(account.positions.values())
    for label, value in (("symbol position", current_symbol_pos), ("total exposure", total_open)):
        if not math.isfinite(value) or value < 0.0:
            return _reject(f"invalid {label} {value!r} in account state")

    # 4. Per-order notional.
    if notional > caps.max_order_notional:
        return _reject(
            f"order notional {notional} > max_order_notional {caps.max_order_notional}"
        )

    # 5. Rolling-24h period notional (turnover).
    period_after = account.rolling_24h_notional + notional
    if period_after > caps.max_period_notional:
        return _reject(
            f"rolling-24h notional {period_after} > max_period_notional {caps.max_period_notional}"
        )

    # 6. Per-symbol position.
    projected_symbol = current_symbol_pos + notional
    if projected_symbol > caps.max_position_per_symbol:
        return _reject(
            f"projected {intent.symbol} position {projected_symbol} > "
            f"max_position_per_symbol {caps.max_position_per_symbol}"
        )

    # 7. Total-exposure HARD_CEILING backstop.
    projected_total = total_open + notional
    if projected_total > caps.hard_ceiling:
        return _reject(
            f"projected total exposure {projected_total} > HARD_CEILING {caps.hard_ceiling}"
        )

    return Decision(
        accepted=True,
        reason="accepted",
        caps_after={
            "order_headroom": caps.max_order_notional - notional,
            "period_remaining": caps.max_period_notional - period_after,
            "symbol_remaining": caps.max_position_per_symbol - projected_symbol,
            "total_remaining": caps.hard_ceiling - projected_total,
        },
    )
