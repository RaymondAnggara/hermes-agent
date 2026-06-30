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

It is intentionally NOT a registered plugin: there is no ``plugin.yaml``, so the
plugin loader will not discover it and no trade tool is exposed on any channel.
Wiring (executors, commands, the service-gated tool, keys) arrives in later,
separately-gated build steps. See ``deploy/tsorf-discord/trading/`` for the
TRADE_SAFETY_DESIGN / STRATEGY_DESIGN this implements.
"""

from __future__ import annotations

from plugins.trading.confidence import (
    CalibrationBand,
    Signal,
    calibrate,
    confidence_p,
    confidence_z,
    sigmoid,
)
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

__all__ = [
    "HARD_CEILING",
    "AccountState",
    "CalibrationBand",
    "Caps",
    "Decision",
    "OrderIntent",
    "Signal",
    "calibrate",
    "clamp_caps",
    "confidence_p",
    "confidence_z",
    "est_notional",
    "risk_gate",
    "sigmoid",
]
