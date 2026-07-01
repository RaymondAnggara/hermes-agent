"""Trading plugin (Phase 5b) — pure decision core only, no I/O / keys / network.

This package is the **seed of a future trading plugin** (footprint ladder: a
plugin, NOT a core tool). BUILD STEP 1 ships only the parts that touch neither
money, keys, the exchange, nor Discord:

- :mod:`plugins.trading.confidence` — the signed-signal logistic confidence
  model (a "panel of advisors"), pure functions, no I/O.
- :mod:`plugins.trading.risk_gate` — the fail-closed, in-code risk gate that
  every order must pass, pure functions, no I/O.
- :mod:`plugins.trading.schema` — the ``trading.db`` SQLite schema (separate
  from ``state.db`` / ``kanban.db``).

Build step 2 adds the order parser + planner (:mod:`plugins.trading.orders`),
the ``trading.db`` data-access layer (:mod:`plugins.trading.store`) — which
derives the account state the risk gate consumes — and the read-only registry /
journal command handlers (:mod:`plugins.trading.commands`).

It is intentionally NOT a registered plugin: there is no ``plugin.yaml``, so the
plugin loader will not discover it and no trade tool is exposed on any channel.
Discord wiring (the service-gated ``/trade`` flow on ``#trading``), executors,
and keys arrive in later, separately-gated build steps. See
``deploy/tsorf-discord/trading/`` for the TRADE_SAFETY_DESIGN / STRATEGY_DESIGN
this implements.
"""

from __future__ import annotations

from plugins.trading.advisor import market_read
from plugins.trading.confidence import (
    CalibrationBand,
    Signal,
    calibrate,
    confidence_p,
    confidence_z,
    sigmoid,
)
from plugins.trading.executor import FillResult, PaperExecutor, paper_trade
from plugins.trading.marketdata import (
    DATA_SOURCE,
    Candle,
    MarketDataError,
    coingecko_id,
    fetch_ohlc,
    parse_ohlc,
)
from plugins.trading.signals import compute_signals
from plugins.trading.orders import (
    OrderParseError,
    OrderPlan,
    RiskRejected,
    parse_order,
    plan_order,
)
from plugins.trading.proposals import propose_order
from plugins.trading.risk_gate import (
    HARD_CEILING,
    AccountState,
    Caps,
    Decision,
    OrderIntent,
    clamp_caps,
    est_notional,
    risk_gate,
)
from plugins.trading.store import TradingStore

__all__ = [
    "DATA_SOURCE",
    "HARD_CEILING",
    "AccountState",
    "CalibrationBand",
    "Candle",
    "Caps",
    "Decision",
    "FillResult",
    "MarketDataError",
    "OrderIntent",
    "OrderParseError",
    "OrderPlan",
    "PaperExecutor",
    "RiskRejected",
    "Signal",
    "TradingStore",
    "calibrate",
    "clamp_caps",
    "coingecko_id",
    "compute_signals",
    "confidence_p",
    "confidence_z",
    "est_notional",
    "fetch_ohlc",
    "market_read",
    "paper_trade",
    "parse_order",
    "parse_ohlc",
    "plan_order",
    "propose_order",
    "risk_gate",
    "sigmoid",
]
