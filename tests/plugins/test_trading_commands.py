"""Tests for the read-only / registry command handlers (commands.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from plugins.trading.commands import (
    cmd_add_strat,
    cmd_add_symbol,
    cmd_strat_list,
    cmd_strat_to_symbol_confidence,
    cmd_symbol_list,
    cmd_trade_journal,
    parse_strat_params,
)
from plugins.trading.store import TradingStore

NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = TradingStore.open(tmp_path / "trading.db")
    yield s
    s.close()


def test_parse_strat_params_coerces_numbers():
    assert parse_strat_params(["tp=2.0", "sl=1.0", "rule=ma_cross"]) == {
        "tp": 2.0,
        "sl": 1.0,
        "rule": "ma_cross",
    }


def test_parse_strat_params_rejects_bad_token():
    with pytest.raises(ValueError):
        parse_strat_params(["tponly"])
    with pytest.raises(ValueError):
        parse_strat_params(["=2.0"])


def test_cmd_add_symbol_reports_added_flag(store):
    assert cmd_add_symbol(store, "btc") == {"ok": True, "symbol": "BTC", "added": True}
    assert cmd_add_symbol(store, "BTC")["added"] is False


def test_cmd_add_strat_and_duplicate(store):
    r = cmd_add_strat(store, "s1", {"tp": 2.0})
    assert r["ok"] and r["id"] > 0
    dup = cmd_add_strat(store, "s1", {"tp": 3.0})
    assert dup["ok"] is False
    assert "already exists" in dup["error"]


def test_cmd_symbol_and_strat_list(store):
    cmd_add_symbol(store, "BTC")
    cmd_add_strat(store, "s1", {"tp": 2.0})
    assert [s["symbol"] for s in cmd_symbol_list(store)["symbols"]] == ["BTC"]
    assert [s["name"] for s in cmd_strat_list(store)["strategies"]] == ["s1"]


def test_confidence_no_signals_returns_none_with_sample_count(store):
    r = cmd_strat_to_symbol_confidence(store, "BTC")
    assert r["raw_p"] is None
    assert r["n_signals"] == 0
    assert r["n_samples"] == 0
    assert r["calibrated"] is False
    assert "no signals" in r["note"]


def test_confidence_with_signals_is_computed_and_uncalibrated(store):
    # A trusted bearish market signal → p < 0.5 (matches the confidence model).
    store.record_signal("BTC", "Igw", 0.3, 0.5, now=NOW)
    store.record_signal("BTC", "Ms", -0.7, 1.0, now=NOW)
    r = cmd_strat_to_symbol_confidence(store, "BTC")
    assert r["n_signals"] == 2
    assert r["raw_p"] < 0.5
    assert r["calibrated"] is False
    assert r["calibrated_p"] == r["raw_p"]  # no bands → identity


def test_confidence_always_carries_sample_count(store):
    """Honesty contract: a number never ships without how much data backs it."""
    store.record_signal("BTC", "Ms", 0.5, 1.0, now=NOW)
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=1.0, outcome="TP", now=NOW)
    store.record_trade("paper", "BTC", "buy", 1.0, entry_px=1.0, outcome="SL", now=NOW)
    r = cmd_strat_to_symbol_confidence(store, "BTC")
    assert r["raw_p"] is not None
    assert r["n_samples"] == 2  # resolved trades behind the number


def test_confidence_applies_calibration_when_bands_exist(store):
    store.record_signal("BTC", "Ms", 2.0, 1.0, now=NOW)  # strongly bullish → high raw_p
    store.conn.execute(
        "INSERT INTO calibration (version, band_lo, band_hi, realized, n_samples, updated_at) "
        "VALUES (1, 0.0, 1.0, 0.58, 300, 't')"
    )
    store.conn.commit()
    r = cmd_strat_to_symbol_confidence(store, "BTC")
    assert r["calibrated"] is True
    assert r["calibrated_p"] == 0.58  # remapped to the band's realized win-rate
    assert r["raw_p"] != r["calibrated_p"]


def test_cmd_trade_journal_sums_pnl_and_flags_benchmark(store):
    store.record_trade(
        "paper", "BTC", "buy", 1.0, entry_px=10.0, realized_pnl=1.5, outcome="TP", now=NOW
    )
    store.record_trade(
        "paper", "ETH", "buy", 1.0, entry_px=5.0, realized_pnl=-0.5, outcome="SL", now=NOW
    )
    r = cmd_trade_journal(store, 10)
    assert r["count"] == 2
    assert r["realized_pnl"] == pytest.approx(1.0)
    assert r["benchmark"] is None  # honestly not computed yet
