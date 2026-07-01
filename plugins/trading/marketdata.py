"""Read-only market-data fetch from CoinGecko (Phase 5b build step 4a).

The ONLY networked module in :mod:`plugins.trading`. Fetches recent OHLC candles
for a symbol from CoinGecko's public API (no key, read-only).

WHY CoinGecko and not the exchange feed (locked decision, 2026-07-01): the egress
proxy allowlists by HOST, not URL path, so opening an exchange's host for prices
would also open its order-placement endpoints. CoinGecko is a **data-only**
provider — its host exposes no trading endpoints, so allowlisting it can never
move money. The exchange trading host stays denied until the live step, at which
point we switch this feed to the exchange's own klines and re-calibrate on it.

All HTTP goes through the container's egress proxy: httpx is created with
``trust_env=True`` so it honors ``HTTPS_PROXY``. ``api.coingecko.com`` must be on
the proxy allowlist (``deploy/tsorf-discord/containment/network/allowlist.filter``)
or every fetch fails closed.

Fail-closed: any network / shape error raises :class:`MarketDataError`; callers
treat a missing feed as "no read", never as a fabricated price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DATA_SOURCE = "coingecko"

# Only symbols we might actually trade are mapped — an unmapped symbol fails
# closed (we do not fetch data for things outside the trading allowlist).
_SYMBOL_IDS = {"BTC": "bitcoin", "ETH": "ethereum"}


class MarketDataError(Exception):
    """Market data could not be fetched or parsed (fail-closed)."""


@dataclass(frozen=True)
class Candle:
    """One OHLC candle. ``ts_ms`` is the CoinGecko epoch-millis open time."""

    ts_ms: int
    open: float
    high: float
    low: float
    close: float


def coingecko_id(symbol: str) -> str:
    """Map a trading symbol to its CoinGecko coin id, or fail closed."""
    sid = _SYMBOL_IDS.get(symbol.upper())
    if not sid:
        raise MarketDataError(f"no CoinGecko id mapping for symbol {symbol!r}")
    return sid


def parse_ohlc(payload: Any) -> list[Candle]:
    """Parse a CoinGecko ``/ohlc`` response (``[[ts,o,h,l,c], ...]``) → candles.

    Pure (no I/O) so it is fully unit-testable from a fixture. Fail-closed on an
    empty/mis-shaped payload or any non-finite / non-positive price.
    """
    if not isinstance(payload, list) or not payload:
        raise MarketDataError("CoinGecko OHLC payload empty or not a list")
    candles: list[Candle] = []
    for row in payload:
        try:
            ts, o, h, low, c = row[0], row[1], row[2], row[3], row[4]
            candle = Candle(int(ts), float(o), float(h), float(low), float(c))
        except (TypeError, ValueError, IndexError) as e:
            raise MarketDataError(f"malformed OHLC row {row!r}: {e}") from None
        if not all(
            math.isfinite(x) and x > 0.0
            for x in (candle.open, candle.high, candle.low, candle.close)
        ):
            raise MarketDataError(f"non-positive/non-finite price in OHLC row {row!r}")
        candles.append(candle)
    return candles


def fetch_ohlc(
    symbol: str,
    *,
    days: int = 30,
    timeout: float = 10.0,
    client: Any = None,
) -> list[Candle]:
    """Fetch recent OHLC candles for ``symbol`` from CoinGecko (read-only).

    ``days=30`` yields ~4h candles (CoinGecko's auto granularity for that range),
    enough history for the trend/momentum signals. Raises
    :class:`MarketDataError` on any failure — never returns a partial or
    fabricated series. ``client`` (an ``httpx.Client``) is injectable for tests.
    """
    sid = coingecko_id(symbol)
    url = f"{COINGECKO_BASE}/coins/{sid}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}

    owns_client = client is None
    if client is None:
        import httpx  # local import: keep module import light, match web_tools

        client = httpx.Client(timeout=timeout, trust_env=True)  # honor HTTPS_PROXY
    try:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except MarketDataError:
        raise
    except Exception as e:  # network error, non-2xx, bad JSON — all fail closed
        raise MarketDataError(
            f"CoinGecko fetch failed for {symbol}: {type(e).__name__}: {e}"
        ) from None
    finally:
        if owns_client:
            client.close()
    return parse_ohlc(payload)
