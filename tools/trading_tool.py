"""The service-gated ``trade`` model tool (Phase 5b build step 3b).

Wires the paper-mode order path (:mod:`plugins.trading`) to the model as a
**single, service-gated tool** bound to the dedicated ``#trading`` channel. This
is the one "core model tool" the trading design deliberately accepts (the
footprint ladder's expensive exception), justified by TWO independent gates that
both have to be true for the model to ever see it:

1. ``check_fn`` (:func:`_check_trading_enabled`): the tool is ABSENT from the
   schema unless ``trading.enabled: true`` AND usable caps are configured in
   ``config.yaml`` — unconfigured = no tool (fail-closed).
2. ``channel_toolsets`` binds the ``trading`` toolset ONLY to ``#trading`` (see
   ``deploy/tsorf-discord/channel-modes.example.yaml``), so even when enabled the
   tool is offered on that one channel and nowhere else.

The handler runs the FULL paper path via :func:`plugins.trading.paper_trade`
(parse → derive account state from the audit store → risk gate → simulated
fill). No exchange, no keys, no network — **paper mode only**. Live execution
(Bybit demo → tiny live, with the ``/trade-confirm`` per-order arm) is a later,
separately-gated step and is NOT reachable from here.
"""

from __future__ import annotations

import logging
from typing import Any

from plugins.trading import (
    HARD_CEILING,
    Caps,
    MarketDataError,
    TradingStore,
    clamp_caps,
    fetch_ohlc,
    market_read,
    paper_trade,
)
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


def _trading_config() -> dict:
    """Return the ``trading`` section of the runtime config, or ``{}``.

    Read fresh (TTL-cached at the registry check_fn layer) so flipping
    ``trading.enabled`` in config takes effect without a code change.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        return {}
    section = cfg.get("trading")
    return section if isinstance(section, dict) else {}


def _load_caps(cfg: dict | None = None) -> Caps | None:
    """Build clamped :class:`Caps` from ``trading.caps``, or ``None`` if unusable.

    Returning ``None`` (rather than a permissive default) preserves the
    fail-closed contract: :func:`paper_trade` rejects every order when caps are
    ``None``. ``clamp_caps`` guarantees config can only tighten, never exceed the
    absolute :data:`~plugins.trading.HARD_CEILING`.
    """
    if cfg is None:
        cfg = _trading_config()
    raw = cfg.get("caps")
    if not isinstance(raw, dict):
        return None
    try:
        symbols = tuple(
            str(s).upper().strip() for s in (raw.get("allowed_symbols") or ())
        )
        caps = Caps(
            max_order_notional=float(raw["max_order_notional"]),
            max_position_per_symbol=float(raw["max_position_per_symbol"]),
            max_period_notional=float(raw["max_period_notional"]),
            allowed_symbols=symbols,
            hard_ceiling=float(raw["hard_ceiling"]) if "hard_ceiling" in raw else HARD_CEILING,
        )
    except (KeyError, TypeError, ValueError):
        return None
    return clamp_caps(caps)


def _check_trading_enabled() -> bool:
    """Service gate: the ``trade`` tool exists only when trading is positively
    enabled AND caps are configured. Anything short of that = no tool."""
    cfg = _trading_config()
    if not cfg.get("enabled"):
        return False
    return _load_caps(cfg) is not None


TRADE_SCHEMA = {
    "name": "trade",
    "description": (
        "Place a PAPER (simulated) order on the #trading desk. Runs the full "
        "safety path: parse -> risk gate (per-order / per-symbol / rolling-24h / "
        "total-exposure caps + a symbol allowlist) -> simulated fill recorded to "
        "the append-only trading audit log. PAPER MODE ONLY: no real money, no "
        "exchange, no keys. A rejected order (bad command, cap exceeded, "
        "disallowed symbol) changes NOTHING and returns the reason. Grammar: "
        "'<side> <qty> <symbol> <type> [<price>]', e.g. 'buy 0.01 BTC limit "
        "60000' or 'sell 0.5 ETH limit 3000'. Market orders need a live price "
        "feed (unavailable in paper) -> use limit orders with an explicit price."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "order": {
                "type": "string",
                "description": (
                    "The order command, e.g. 'buy 0.01 BTC limit 60000'. "
                    "Grammar: <side> <qty> <symbol> <type> [<price>]."
                ),
            },
        },
        "required": ["order"],
    },
}


def _handle_trade(args: dict, **kwargs: Any) -> str:
    """Run one order through the full paper path and return its structured result."""
    text = (args or {}).get("order")
    if not text or not isinstance(text, str):
        return tool_error(
            "trade requires an 'order' string, e.g. 'buy 0.01 BTC limit 60000'"
        )

    caps = _load_caps()
    if caps is None:
        return tool_error(
            "trading caps are not configured (fail-closed) — refusing all orders. "
            "Set trading.caps in config.yaml."
        )

    store = TradingStore.open()
    try:
        result = paper_trade(text, store, caps)
    finally:
        store.close()

    if result.get("status") == "rejected":
        logger.info(
            "PAPER trade REJECTED (%s): %s", result.get("stage"), result.get("reason")
        )
    else:
        logger.info(
            "PAPER trade FILLED: %s %s %s @ %s (plan %s)",
            result.get("side"),
            result.get("qty"),
            result.get("symbol"),
            result.get("fill_px"),
            result.get("plan_id"),
        )
    return tool_result(result)


registry.register(
    name="trade",
    toolset="trading",
    schema=TRADE_SCHEMA,
    handler=_handle_trade,
    check_fn=_check_trading_enabled,
    emoji="📈",
)


MARKET_READ_SCHEMA = {
    "name": "market_read",
    "description": (
        "Get a READ-ONLY market read on an allowed symbol (BTC or ETH): fetches "
        "recent prices from CoinGecko, computes trend + momentum signals, and "
        "runs them through the confidence model. Returns a directional lean "
        "(bullish/bearish/neutral), a confidence %, the signals behind it, and "
        "ALWAYS `n_samples` + `calibrated` — the number is UNCALIBRATED today "
        "(not yet validated against real outcomes), so present it as decision "
        "support, not a reliable probability. This places NO order and moves no "
        "money; it is research only. Cite it honestly, including the caveat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "The symbol to read, e.g. 'BTC' or 'ETH'.",
            },
        },
        "required": ["symbol"],
    },
}


def _handle_market_read(args: dict, **kwargs: Any) -> str:
    """Fetch data + compute a read for one symbol. Read-only; fail-closed."""
    symbol = (args or {}).get("symbol")
    if not symbol or not isinstance(symbol, str):
        return tool_error("market_read requires a 'symbol', e.g. 'BTC'")
    symbol = symbol.upper().strip()

    # Scope reads to symbols we might actually trade (the caps allowlist), so the
    # advisor never researches things outside the trading universe.
    caps = _load_caps()
    if caps is not None and symbol not in caps.allowed_symbols:
        return tool_error(
            f"symbol {symbol} not in allowlist {caps.allowed_symbols}"
        )

    try:
        candles = fetch_ohlc(symbol)
    except MarketDataError as e:
        return tool_error(f"market data unavailable: {e}")

    store = TradingStore.open()
    try:
        read = market_read(symbol, candles, store)
    finally:
        store.close()

    logger.info(
        "market_read %s: lean=%s p=%.3f (calibrated=%s, n=%s)",
        symbol,
        read.get("lean"),
        read.get("confidence_p") or 0.0,
        read.get("calibrated"),
        read.get("n_samples"),
    )
    return tool_result(read)


registry.register(
    name="market_read",
    toolset="trading",
    schema=MARKET_READ_SCHEMA,
    handler=_handle_market_read,
    check_fn=_check_trading_enabled,
    emoji="📊",
)
