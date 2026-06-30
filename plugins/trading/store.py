"""``trading.db`` data-access layer (STRATEGY_DESIGN §7 / TRADE_SAFETY_DESIGN §9).

Local SQLite only — no network, no keys, no exchange contact. Provides:

- the symbol/strategy **registry** (read + add; no deletes — append-only spirit),
- **append-only** audit writes (``signals``, ``predictions``, ``trades``),
- :meth:`TradingStore.account_state`, which derives the rolling-24h notional and
  per-symbol open exposure that :func:`~plugins.trading.risk_gate.risk_gate`
  consumes — so the gate's numbers come from the durable audit trail, not from a
  caller's in-memory guess.

Timestamps are stored as timezone-aware UTC ISO-8601 (``datetime.now(timezone.utc)
.isoformat()``); since the format is fixed and always UTC, lexical comparison of
the strings is a valid time comparison (used for the rolling-24h window).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from plugins.trading.risk_gate import AccountState
from plugins.trading.schema import init_trading_db

# Trade outcomes that mean the position is still open (counts toward exposure).
_OPEN_OUTCOMES = (None, "open")
# Trade outcomes that mean the trade resolved (counts as a trained sample).
_RESOLVED_OUTCOMES = ("TP", "SL", "timeout")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TradingStore:
    """Thin data-access wrapper over a ``trading.db`` connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str | Path | None = None) -> TradingStore:
        """Open (creating + migrating if needed) ``trading.db`` and wrap it."""
        return cls(init_trading_db(path))

    def close(self) -> None:
        self.conn.close()

    # ---- registry -------------------------------------------------------

    def add_symbol(self, symbol: str, note: str | None = None, *, now: datetime | None = None) -> bool:
        """Watch a symbol. Returns True if newly added, False if already watched."""
        symbol = symbol.upper()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol, added_at, note) VALUES (?, ?, ?)",
            (symbol, _iso(now or _utc_now()), note),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_symbols(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT symbol, added_at, note FROM symbols ORDER BY symbol"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_strategy(
        self, name: str, params: dict[str, Any], *, now: datetime | None = None
    ) -> int:
        """Register a strategy. Raises ValueError on a duplicate name."""
        try:
            cur = self.conn.execute(
                "INSERT INTO strategies (name, params_json, created_at) VALUES (?, ?, ?)",
                (name, json.dumps(params, sort_keys=True), _iso(now or _utc_now())),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"strategy {name!r} already exists") from e
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_strategies(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT id, name, params_json, created_at, active FROM strategies"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY name"
        out: list[dict[str, Any]] = []
        for r in self.conn.execute(sql).fetchall():
            d = dict(r)
            d["params"] = json.loads(d.pop("params_json"))
            out.append(d)
        return out

    # ---- append-only audit writes --------------------------------------

    def record_signal(
        self,
        symbol: str,
        signal_type: str,
        s: float,
        weight: float,
        *,
        source_ref: str | None = None,
        now: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals (decided_at, symbol, signal_type, source_ref, s, weight) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_iso(now or _utc_now()), symbol.upper(), signal_type, source_ref, s, weight),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def record_prediction(
        self,
        symbol: str,
        z: float,
        raw_p: float,
        calibrated_p: float,
        action: str,
        *,
        strategy_id: int | None = None,
        threshold: float | None = None,
        now: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO predictions "
            "(decided_at, symbol, strategy_id, z, raw_p, calibrated_p, threshold, action) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(now or _utc_now()),
                symbol.upper(),
                strategy_id,
                z,
                raw_p,
                calibrated_p,
                threshold,
                action,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def record_trade(
        self,
        mode: str,
        symbol: str,
        side: str,
        qty: float,
        *,
        entry_px: float | None = None,
        exit_px: float | None = None,
        fees: float | None = None,
        realized_pnl: float | None = None,
        outcome: str | None = "open",
        prediction_id: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO trades "
            "(created_at, mode, prediction_id, symbol, side, qty, entry_px, exit_px, "
            " fees, realized_pnl, outcome, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(now or _utc_now()),
                mode,
                prediction_id,
                symbol.upper(),
                side,
                qty,
                entry_px,
                exit_px,
                fees,
                realized_pnl,
                outcome,
                idempotency_key,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    # ---- derived account state (feeds the risk gate) -------------------

    def account_state(self, *, now: datetime | None = None, window_hours: float = 24.0) -> AccountState:
        """Derive :class:`AccountState` from the durable audit trail.

        - ``rolling_24h_notional`` = Σ ``qty * entry_px`` of trades created
          within the trailing ``window_hours`` (turnover; both sides count).
        - ``positions`` = per-symbol net open notional of trades whose outcome is
          still open (buys add, sells subtract), floored at 0 and dropping any
          symbol that nets to ≤ 0. Conservative by construction — see the gate.
        """
        now = now or _utc_now()
        cutoff = _iso(now - timedelta(hours=window_hours))

        roll_row = self.conn.execute(
            "SELECT COALESCE(SUM(qty * entry_px), 0.0) AS turnover FROM trades "
            "WHERE entry_px IS NOT NULL AND created_at >= ?",
            (cutoff,),
        ).fetchone()
        rolling = float(roll_row["turnover"])

        placeholders = ", ".join("?" for _ in _OPEN_OUTCOMES if _ is not None)
        # Open = outcome IS NULL OR outcome IN ('open', ...).
        open_rows = self.conn.execute(
            "SELECT symbol, side, COALESCE(SUM(qty * entry_px), 0.0) AS notional FROM trades "
            "WHERE entry_px IS NOT NULL "
            f"  AND (outcome IS NULL OR outcome IN ({placeholders})) "
            "GROUP BY symbol, side",
            tuple(o for o in _OPEN_OUTCOMES if o is not None),
        ).fetchall()

        net: dict[str, float] = {}
        for r in open_rows:
            signed = float(r["notional"]) * (1.0 if r["side"] == "buy" else -1.0)
            net[r["symbol"]] = net.get(r["symbol"], 0.0) + signed
        positions = {sym: val for sym, val in net.items() if val > 0.0}

        return AccountState(positions=positions, rolling_24h_notional=rolling)

    # ---- reads for the journal / confidence command --------------------

    def recent_trades(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(0, limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_signals(self, symbol: str) -> list[dict[str, Any]]:
        """Most recent signal of each type for ``symbol`` (one per signal_type)."""
        rows = self.conn.execute(
            "SELECT s1.signal_type, s1.s, s1.weight, s1.decided_at, s1.source_ref "
            "FROM signals s1 "
            "JOIN (SELECT signal_type, MAX(decided_at) AS md FROM signals "
            "      WHERE symbol = ? GROUP BY signal_type) s2 "
            "  ON s1.signal_type = s2.signal_type AND s1.decided_at = s2.md "
            "WHERE s1.symbol = ? ORDER BY s1.signal_type",
            (symbol.upper(), symbol.upper()),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolved_trade_count(self, symbol: str) -> int:
        placeholders = ", ".join("?" for _ in _RESOLVED_OUTCOMES)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM trades WHERE symbol = ? AND outcome IN ({placeholders})",
            (symbol.upper(), *_RESOLVED_OUTCOMES),
        ).fetchone()
        return int(row["n"])

    def calibration_bands(self) -> list[dict[str, Any]]:
        """Bands of the highest calibration version (empty until trained)."""
        ver = self.conn.execute("SELECT MAX(version) AS v FROM calibration").fetchone()["v"]
        if ver is None:
            return []
        rows = self.conn.execute(
            "SELECT band_lo, band_hi, realized, n_samples FROM calibration "
            "WHERE version = ? ORDER BY band_lo",
            (ver,),
        ).fetchall()
        return [dict(r) for r in rows]
