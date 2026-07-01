"""Assemble a read-only market read (Phase 5b build step 4a).

Ties market data → signals → the confidence model into ONE structured read for
the #trading advisory tool. Read-only: it computes and (optionally) records the
signals to the audit store, but places NO orders.

Honesty contract (STRATEGY_DESIGN §0/§8): the read ALWAYS carries ``n_samples``
+ ``calibrated`` status, so a confidence number never ships without how much data
backs it and whether it has been validated against real outcomes. It has not yet
— calibration bands come from resolved trades (build step 4c) — so today
``calibrated`` is False and the read says so plainly. Decision support, not a
prediction to trust blindly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from plugins.trading.confidence import (
    CalibrationBand,
    calibrate,
    confidence_p,
    confidence_z,
)
from plugins.trading.marketdata import DATA_SOURCE, Candle
from plugins.trading.signals import compute_signals
from plugins.trading.store import TradingStore

# Confidence must clear this margin off a coin-flip to read as directional.
_BULL = 0.55
_BEAR = 0.45


def _lean(p: float) -> str:
    if p >= _BULL:
        return "bullish"
    if p <= _BEAR:
        return "bearish"
    return "neutral"


def market_read(
    symbol: str,
    candles: Sequence[Candle],
    store: TradingStore | None = None,
    *,
    record: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute a structured, honest market read for ``symbol``.

    Signals are computed from ``candles``; the confidence model turns them into a
    lean + probability; calibration (if any bands exist) remaps it to realized
    win-rate. When ``store`` is given and ``record`` is True, each computed signal
    is appended to the audit store tagged ``source_ref=DATA_SOURCE`` — building
    the history that 4c's calibration/benchmark will consume.
    """
    symbol = symbol.upper()
    signals, unavailable = compute_signals(candles)
    z = confidence_z(signals)
    raw_p = confidence_p(signals)

    bands: list[CalibrationBand] = []
    n_samples = 0
    if store is not None:
        bands = [
            CalibrationBand(b["band_lo"], b["band_hi"], b["realized"])
            for b in store.calibration_bands()
        ]
        n_samples = store.resolved_trade_count(symbol)
    calibrated = bool(bands)
    cal_p = calibrate(raw_p, bands)

    if record and store is not None:
        for sig in signals:
            store.record_signal(
                symbol, sig.name, sig.s, sig.w, source_ref=DATA_SOURCE, now=now
            )

    return {
        "symbol": symbol,
        "as_of_ms": candles[-1].ts_ms if candles else None,
        "last_price": candles[-1].close if candles else None,
        "data_source": DATA_SOURCE,
        "lean": _lean(cal_p),
        "confidence_p": cal_p,
        "raw_confidence_p": raw_p,
        "z": z,
        "signals": [
            {"name": s.name, "s": round(s.s, 4), "weight": s.w} for s in signals
        ],
        "unavailable_signals": unavailable,
        "n_samples": n_samples,
        "calibrated": calibrated,
        "note": (
            None
            if calibrated
            else (
                "UNCALIBRATED: this % is the raw model, not yet validated against "
                "real outcomes (needs resolved trades — build step 4c). Decision "
                "support only, not a reliable probability."
            )
        ),
    }
