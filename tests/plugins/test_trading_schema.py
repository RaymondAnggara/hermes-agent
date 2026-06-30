"""Tests for the ``trading.db`` schema (STRATEGY_DESIGN §7).

No network, no keys. We assert the schema's *contracts* (tables exist, the audit
mode/side CHECKs hold, foreign keys are enforceable, init is idempotent), not a
frozen DDL string.
"""

from __future__ import annotations

import sqlite3

import pytest

from plugins.trading.schema import init_trading_db, trading_db_path

EXPECTED_TABLES = {
    "symbols",
    "strategies",
    "signals",
    "predictions",
    "trades",
    "weights",
    "calibration",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_init_creates_all_tables(tmp_path):
    conn = init_trading_db(tmp_path / "trading.db")
    try:
        assert EXPECTED_TABLES <= _table_names(conn)
    finally:
        conn.close()


def test_init_is_idempotent(tmp_path):
    db = tmp_path / "trading.db"
    init_trading_db(db).close()
    conn = init_trading_db(db)  # second call must not raise
    try:
        assert EXPECTED_TABLES <= _table_names(conn)
    finally:
        conn.close()


def test_trades_mode_check_constraint(tmp_path):
    conn = init_trading_db(tmp_path / "trading.db")
    try:
        conn.execute(
            "INSERT INTO trades (created_at, mode, symbol, side, qty) "
            "VALUES ('t', 'paper', 'BTC', 'buy', 1.0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trades (created_at, mode, symbol, side, qty) "
                "VALUES ('t', 'mainnet', 'BTC', 'buy', 1.0)"  # not paper|demo|live
            )
    finally:
        conn.close()


def test_trades_side_check_constraint(tmp_path):
    conn = init_trading_db(tmp_path / "trading.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trades (created_at, mode, symbol, side, qty) "
                "VALUES ('t', 'paper', 'BTC', 'short', 1.0)"  # not buy|sell
            )
    finally:
        conn.close()


def test_foreign_keys_enforced(tmp_path):
    conn = init_trading_db(tmp_path / "trading.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # A trade referencing a non-existent prediction must be rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trades (created_at, mode, symbol, side, qty, prediction_id) "
                "VALUES ('t', 'paper', 'BTC', 'buy', 1.0, 9999)"
            )
    finally:
        conn.close()


def test_strategies_name_unique(tmp_path):
    conn = init_trading_db(tmp_path / "trading.db")
    try:
        conn.execute(
            "INSERT INTO strategies (name, params_json, created_at) VALUES ('s1', '{}', 't')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO strategies (name, params_json, created_at) VALUES ('s1', '{}', 't')"
            )
    finally:
        conn.close()


def test_trade_idempotency_key_unique(tmp_path):
    conn = init_trading_db(tmp_path / "trading.db")
    try:
        conn.execute(
            "INSERT INTO trades (created_at, mode, symbol, side, qty, idempotency_key) "
            "VALUES ('t', 'paper', 'BTC', 'buy', 1.0, 'order-1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trades (created_at, mode, symbol, side, qty, idempotency_key) "
                "VALUES ('t', 'paper', 'ETH', 'sell', 2.0, 'order-1')"
            )
    finally:
        conn.close()


def test_default_db_path_under_hermes_home():
    # The autouse _isolate_hermes_home fixture points HERMES_HOME at a temp dir.
    assert trading_db_path().name == "trading.db"
