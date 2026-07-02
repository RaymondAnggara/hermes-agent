"""Daily forecast log — the data-generation tick (Phase 5b step 4c).

Records a read (signals + a directional prediction, when the read leans) for each
symbol, WITHOUT placing any order — so the edge measurement accrues an unbiased,
regularly-sampled forecast history rather than only the calls someone happened to
ask about. NO orders, no money, no fills.

This is the deterministic core meant to run from a ``no_agent`` cron tick once a
day (a thin shim in ``HERMES_HOME/scripts/`` calls it). Keeping it here (not in
the shim) makes it unit-testable and keeps the container script trivial.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from plugins.trading.marketdata import MarketDataError, fetch_ohlc
from plugins.trading.proposals import propose_order
from plugins.trading.risk_gate import Caps
from plugins.trading.store import TradingStore


def record_daily_reads(
    store: TradingStore,
    caps: Caps | None,
    symbols: Sequence[str],
    *,
    fetch: Callable[..., Any] = fetch_ohlc,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Log a read per symbol (signals always; a prediction when directional).

    Reuses :func:`~plugins.trading.proposals.propose_order` with ``record=True``
    and simply discards the proposal — nothing is ever filled. Returns a summary
    per symbol; a data-fetch failure for one symbol is captured, not raised, so a
    single bad feed doesn't skip the rest.
    """
    results: dict[str, Any] = {}
    for sym in symbols:
        sym = sym.upper()
        try:
            candles = fetch(sym)
        except MarketDataError as e:
            results[sym] = {"ok": False, "error": str(e)}
            continue
        res = propose_order(sym, candles, store, caps, record=True, now=now)
        read = res.get("read", {})
        results[sym] = {
            "ok": True,
            "lean": read.get("lean"),
            "confidence_p": read.get("confidence_p"),
            "prediction_id": res.get("prediction_id"),  # None on a neutral (abstained) read
        }
    return results
