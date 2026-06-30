"""Tests for the trading.db data-access layer (store.py).

The account-state derivation is the safety-critical part — it is what the risk
gate trusts — so it gets the most adversarial coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plugins.trading.store import TradingStore

NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = TradingStore.open(tmp_path / "trading.db")
    yield s
    s.close()


# ---- registry ----------------------------------------------------------


def test_add_symbol_is_idempotent(store):
    assert store.add_symbol("btc") is True  # newly added (and upcased)
    assert store.add_symbol("BTC") is False  # already watched
    assert [r["symbol"] for r in store.list_symbols()] == ["BTC"]


def test_add_strategy_and_duplicate(store):
    sid = store.add_strategy("ma_cross", {"tp": 2.0, "sl": 1.0})
    assert sid > 0
    with pytest.raises(ValueError):
        store.add_strategy("ma_cross", {"tp": 3.0})
    strategies = store.list_strategies()
    assert len(strategies) == 1
    assert strategies[0]["name"] == "ma_cross"
    assert strategies[0]["params"] == {"tp": 2.0, "sl": 1.0}  # round-trips


# ---- account state (feeds the risk gate) -------------------------------


def test_rolling_24h_window_excludes_old_trades(store):
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=10.0, now=NOW)  # in window
    store.record_trade(
        "paper", "BTC", "buy", 1.0, entry_px=10.0, now=NOW - timedelta(hours=25)
    )  # out of window
    state = store.account_state(now=NOW)
    assert state.rolling_24h_notional == pytest.approx(10.0)


def test_open_positions_net_buys_minus_sells_floored(store):
    # BTC: buy 20 + sell 10 (both open) → net +10
    store.record_trade("paper", "BTC", "buy", 2.0, entry_px=10.0, now=NOW)
    store.record_trade("paper", "BTC", "sell", 1.0, entry_px=10.0, now=NOW)
    # ETH: buy 5 + sell 10 → net -5 → dropped (not negative exposure)
    store.record_trade("paper", "ETH", "buy", 1.0, entry_px=5.0, now=NOW)
    store.record_trade("paper", "ETH", "sell", 2.0, entry_px=5.0, now=NOW)
    state = store.account_state(now=NOW)
    assert state.positions == {"BTC": pytest.approx(10.0)}


def test_resolved_trades_excluded_from_open_exposure(store):
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=10.0, outcome="TP", now=NOW)
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=10.0, outcome=None, now=NOW)
    state = store.account_state(now=NOW)
    # Only the still-open (NULL outcome) trade counts toward exposure.
    assert state.positions == {"BTC": pytest.approx(10.0)}
    # ...but both count toward 24h turnover.
    assert state.rolling_24h_notional == pytest.approx(20.0)


def test_account_state_empty_db(store):
    state = store.account_state(now=NOW)
    assert state.positions == {}
    assert state.rolling_24h_notional == 0.0


# ---- reads -------------------------------------------------------------


def test_recent_trades_order_and_limit(store):
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=1.0, now=NOW - timedelta(minutes=2))
    store.record_trade("paper", "ETH", "buy", 1.0, entry_px=1.0, now=NOW - timedelta(minutes=1))
    store.record_trade("paper", "BTC", "sell", 1.0, entry_px=1.0, now=NOW)
    rows = store.recent_trades(2)
    assert len(rows) == 2
    assert rows[0]["side"] == "sell"  # most recent first


def test_latest_signals_one_per_type(store):
    store.record_signal("BTC", "Ms", 0.2, 1.0, now=NOW - timedelta(hours=1))
    store.record_signal("BTC", "Ms", -0.5, 1.0, now=NOW)  # newer Ms wins
    store.record_signal("BTC", "Igw", 0.3, 0.5, now=NOW)
    latest = {r["signal_type"]: r["s"] for r in store.latest_signals("BTC")}
    assert latest == {"Ms": -0.5, "Igw": 0.3}


def test_resolved_trade_count(store):
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=1.0, outcome="TP", now=NOW)
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=1.0, outcome="SL", now=NOW)
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=1.0, outcome="open", now=NOW)
    assert store.resolved_trade_count("BTC") == 2  # open one not counted


def test_calibration_bands_empty_then_versioned(store):
    assert store.calibration_bands() == []
    store.conn.execute(
        "INSERT INTO calibration (version, band_lo, band_hi, realized, n_samples, updated_at) "
        "VALUES (1, 0.0, 0.5, 0.30, 100, 't')"
    )
    store.conn.execute(
        "INSERT INTO calibration (version, band_lo, band_hi, realized, n_samples, updated_at) "
        "VALUES (2, 0.0, 1.0, 0.55, 200, 't')"  # newer version
    )
    store.conn.commit()
    bands = store.calibration_bands()
    assert len(bands) == 1  # only the highest version
    assert bands[0]["realized"] == 0.55
