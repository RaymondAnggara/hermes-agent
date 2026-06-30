"""Read-only / registry command handlers (STRATEGY_DESIGN §8).

Pure logic over a :class:`~plugins.trading.store.TradingStore`; these return
**structured results** (plain dicts), not formatted chat strings, so they can be
unit-tested and later rendered by whatever surface invokes them. None of these
place an order or touch keys/network — that is the gated ``/trade`` flow (step 3).

Honesty contract (STRATEGY_DESIGN §0, §12): a confidence number is never shown
without **how much data backs it** and **whether it is calibrated**. So
:func:`cmd_strat_to_symbol_confidence` always returns ``n_samples`` and
``calibrated`` alongside the number, and :func:`cmd_trade_journal` flags that the
vs-benchmark comparison is not computed yet.
"""

from __future__ import annotations

from typing import Any

from plugins.trading.confidence import CalibrationBand, Signal, calibrate, confidence_z, sigmoid
from plugins.trading.store import TradingStore


def parse_strat_params(tokens: list[str]) -> dict[str, Any]:
    """Parse ``key=value`` tokens into a params dict (numbers coerced).

    e.g. ``["tp=2.0", "sl=1.0", "rule=ma_cross"]`` →
    ``{"tp": 2.0, "sl": 1.0, "rule": "ma_cross"}``. Raises ValueError on a token
    without ``=``.
    """
    params: dict[str, Any] = {}
    for tok in tokens:
        if "=" not in tok:
            raise ValueError(f"strategy param {tok!r} must be key=value")
        key, _, raw = tok.partition("=")
        if not key:
            raise ValueError(f"strategy param {tok!r} has an empty key")
        try:
            params[key] = float(raw)
        except ValueError:
            params[key] = raw
    return params


def cmd_add_symbol(store: TradingStore, symbol: str, note: str | None = None) -> dict[str, Any]:
    added = store.add_symbol(symbol, note)
    return {"ok": True, "symbol": symbol.upper(), "added": added}


def cmd_add_strat(store: TradingStore, name: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        sid = store.add_strategy(name, params)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "name": name, "id": sid, "params": params}


def cmd_symbol_list(store: TradingStore) -> dict[str, Any]:
    return {"ok": True, "symbols": store.list_symbols()}


def cmd_strat_list(store: TradingStore) -> dict[str, Any]:
    return {"ok": True, "strategies": store.list_strategies()}


def cmd_strat_to_symbol_confidence(
    store: TradingStore, symbol: str, strategy: str | None = None
) -> dict[str, Any]:
    """Current confidence for ``symbol`` from its latest signals, with data backing.

    Returns ``raw_p`` (model output), ``calibrated_p`` (remapped to realized
    win-rate if bands exist), ``z``, ``n_signals`` used, ``n_samples`` (resolved
    trades behind the number), and ``calibrated`` (whether a calibration map
    applied). With no signals yet, ``raw_p`` is ``None`` and a note explains why.
    """
    sig_rows = store.latest_signals(symbol)
    n_samples = store.resolved_trade_count(symbol)
    bands = [
        CalibrationBand(b["band_lo"], b["band_hi"], b["realized"]) for b in store.calibration_bands()
    ]

    if not sig_rows:
        return {
            "ok": True,
            "symbol": symbol.upper(),
            "strategy": strategy,
            "raw_p": None,
            "calibrated_p": None,
            "z": None,
            "n_signals": 0,
            "n_samples": n_samples,
            "calibrated": bool(bands),
            "note": "no signals recorded yet for this symbol",
        }

    signals = [Signal(r["signal_type"], r["s"], r["weight"]) for r in sig_rows]
    z = confidence_z(signals)
    raw_p = sigmoid(z)
    calibrated_p = calibrate(raw_p, bands)
    return {
        "ok": True,
        "symbol": symbol.upper(),
        "strategy": strategy,
        "raw_p": raw_p,
        "calibrated_p": calibrated_p,
        "z": z,
        "n_signals": len(signals),
        "n_samples": n_samples,
        "calibrated": bool(bands),
    }


def cmd_trade_journal(store: TradingStore, n: int = 10) -> dict[str, Any]:
    """Recent trades + running realized P&L. Benchmark comparison is not computed
    yet (it arrives with the validation report, step 4) — flagged honestly."""
    trades = store.recent_trades(n)
    realized = sum(t["realized_pnl"] for t in trades if t["realized_pnl"] is not None)
    return {
        "ok": True,
        "trades": trades,
        "count": len(trades),
        "realized_pnl": realized,
        "benchmark": None,
        "note": "vs-benchmark (buy-and-hold / random) not computed yet — see STRATEGY_DESIGN §6",
    }
