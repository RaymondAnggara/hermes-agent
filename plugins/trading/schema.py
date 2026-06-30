"""``trading.db`` SQLite schema — separate from ``state.db`` / ``kanban.db``.

Implements STRATEGY_DESIGN.md §7. Pure DDL + a small idempotent initializer; no
network, no keys, no exchange contact. FTS is not needed here.

Tables: ``symbols``, ``strategies``, ``signals`` (append-only), ``predictions``,
``trades`` (append-only audit, mode ``paper|demo|live``), ``weights`` and
``calibration`` (both versioned so changes are traceable and reversible).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_constants import get_hermes_home

#: Schema version — bump when DDL below changes in a non-additive way.
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS symbols (
    symbol    TEXT PRIMARY KEY,
    added_at  TEXT NOT NULL,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS strategies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    params_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

-- Append-only: every signal value seen at each decision time.
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    signal_type TEXT NOT NULL,            -- Igw | Itw | Ms | trader
    source_ref  TEXT,                     -- raw source reference (url/id), untrusted
    s           REAL NOT NULL,            -- extracted opinion in [-1, +1]
    weight      REAL NOT NULL             -- trust weight used at decision time
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals(symbol, decided_at);

CREATE TABLE IF NOT EXISTS predictions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    strategy_id  INTEGER,
    z            REAL NOT NULL,
    raw_p        REAL NOT NULL,
    calibrated_p REAL NOT NULL,
    threshold    REAL,
    action       TEXT NOT NULL,           -- proposed: buy | sell | hold | skip
    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
);
CREATE INDEX IF NOT EXISTS idx_predictions_symbol_time ON predictions(symbol, decided_at);

-- Append-only audit (mirrors TRADE_SAFETY_DESIGN §9). mode in paper|demo|live.
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('paper', 'demo', 'live')),
    prediction_id   INTEGER,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty             REAL NOT NULL,
    entry_px        REAL,
    exit_px         REAL,
    fees            REAL,
    realized_pnl    REAL,
    outcome         TEXT,                 -- TP | SL | timeout | open
    idempotency_key TEXT UNIQUE,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_time ON trades(symbol, created_at);

-- Versioned learned weights (which advisors to trust). One row per
-- (version, signal_type); the highest version is current.
CREATE TABLE IF NOT EXISTS weights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    weight      REAL NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (version, signal_type)
);

-- Versioned calibration map (raw confidence band -> realized win-rate).
CREATE TABLE IF NOT EXISTS calibration (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version    INTEGER NOT NULL,
    band_lo    REAL NOT NULL,
    band_hi    REAL NOT NULL,
    realized   REAL NOT NULL,
    n_samples  INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def trading_db_path() -> Path:
    """Path to ``trading.db`` under the Hermes home (sibling of ``state.db``)."""
    return get_hermes_home() / "trading.db"


def init_trading_db(path: str | Path | None = None) -> sqlite3.Connection:
    """Create (idempotently) and return a connection to ``trading.db``.

    Enables WAL + foreign-key enforcement and applies :data:`SCHEMA_SQL`. All
    DDL is ``IF NOT EXISTS`` so calling this on an existing DB is a no-op. The
    caller owns the returned connection and should close it.
    """
    db_path = Path(path) if path is not None else trading_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
