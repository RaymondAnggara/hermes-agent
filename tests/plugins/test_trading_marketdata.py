"""Tests for the read-only CoinGecko market-data module (marketdata.py).

Pure parsing is tested from fixtures; the network fetch is tested through an
injected fake client so no test ever touches the network. Fail-closed behavior
(bad shapes, non-positive prices, network errors) is the focus.
"""

from __future__ import annotations

import pytest

from plugins.trading.marketdata import (
    Candle,
    MarketDataError,
    coingecko_id,
    fetch_ohlc,
    parse_ohlc,
)

# CoinGecko /ohlc shape: [[ts_ms, open, high, low, close], ...]
GOOD = [
    [1_700_000_000_000, 60000.0, 60500.0, 59800.0, 60250.0],
    [1_700_014_400_000, 60250.0, 60800.0, 60100.0, 60700.0],
]


class _FakeResp:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal httpx.Client stand-in; records the URL it was asked for."""

    def __init__(self, resp=None, raise_on_get=None):
        self._resp = resp
        self._raise = raise_on_get
        self.last_url = None
        self.last_params = None
        self.closed = False

    def get(self, url, params=None):
        self.last_url = url
        self.last_params = params
        if self._raise is not None:
            raise self._raise
        return self._resp

    def close(self):
        self.closed = True


# ---- symbol mapping -------------------------------------------------------


def test_coingecko_id_maps_known_symbols():
    assert coingecko_id("btc") == "bitcoin"
    assert coingecko_id("ETH") == "ethereum"


def test_coingecko_id_fails_closed_on_unknown():
    with pytest.raises(MarketDataError):
        coingecko_id("DOGE")


# ---- parsing --------------------------------------------------------------


def test_parse_ohlc_happy():
    candles = parse_ohlc(GOOD)
    assert len(candles) == 2
    assert candles[0] == Candle(1_700_000_000_000, 60000.0, 60500.0, 59800.0, 60250.0)
    assert candles[-1].close == 60700.0


def test_parse_ohlc_rejects_empty_or_wrong_type():
    for bad in ([], {}, None, "nope"):
        with pytest.raises(MarketDataError):
            parse_ohlc(bad)


def test_parse_ohlc_rejects_malformed_row():
    with pytest.raises(MarketDataError):
        parse_ohlc([[1, 2, 3]])  # too few fields


def test_parse_ohlc_rejects_nonpositive_price():
    with pytest.raises(MarketDataError):
        parse_ohlc([[1_700_000_000_000, 60000.0, 60500.0, -1.0, 60250.0]])


# ---- fetch (injected client, no network) ----------------------------------


def test_fetch_ohlc_uses_client_and_parses():
    client = _FakeClient(resp=_FakeResp(GOOD))
    candles = fetch_ohlc("BTC", client=client)
    assert len(candles) == 2
    assert "coins/bitcoin/ohlc" in client.last_url
    assert client.last_params["vs_currency"] == "usd"
    # An injected client is owned by the caller — fetch must NOT close it.
    assert client.closed is False


def test_fetch_ohlc_unknown_symbol_fails_closed():
    with pytest.raises(MarketDataError):
        fetch_ohlc("DOGE", client=_FakeClient(resp=_FakeResp(GOOD)))


def test_fetch_ohlc_wraps_network_error():
    client = _FakeClient(raise_on_get=ConnectionError("no route"))
    with pytest.raises(MarketDataError):
        fetch_ohlc("BTC", client=client)


def test_fetch_ohlc_wraps_http_error():
    client = _FakeClient(resp=_FakeResp(GOOD, status_ok=False))
    with pytest.raises(MarketDataError):
        fetch_ohlc("BTC", client=client)
