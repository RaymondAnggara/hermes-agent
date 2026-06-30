"""Tests for the paper executor + full paper-mode path (executor.py).

Covers the §7 paper-review checklist: the full path runs end-to-end, and every
adversarial rejection (over-cap, bad symbol, negative qty, 24h cap exhaustion)
fires WITHOUT mutating state.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from plugins.trading.executor import PaperExecutor, paper_trade
from plugins.trading.orders import parse_order, plan_order
from plugins.trading.risk_gate import AccountState, Caps
from plugins.trading.store import TradingStore

NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
V1_CAPS = Caps(20.0, 40.0, 100.0, ("BTC", "ETH"), 90.0)


@pytest.fixture
def store(tmp_path):
    s = TradingStore.open(tmp_path / "trading.db")
    yield s
    s.close()


def test_paper_fill_records_audit_trade(store):
    plan = plan_order(parse_order("buy 1 BTC limit 10"), AccountState(), V1_CAPS)
    fill = PaperExecutor(store).execute(plan, now=NOW)
    assert fill.mode == "paper"
    assert fill.fill_px == 10.0
    assert fill.notional == 10.0
    assert fill.replayed is False
    # The fill shows up in the audit trail and in derived exposure.
    assert store.recent_trades(1)[0]["idempotency_key"] == plan.plan_id
    assert store.account_state(now=NOW).positions == {"BTC": pytest.approx(10.0)}


def test_paper_fill_is_idempotent_on_replay(store):
    plan = plan_order(parse_order("buy 1 BTC limit 10"), AccountState(), V1_CAPS)
    ex = PaperExecutor(store)
    first = ex.execute(plan, now=NOW)
    second = ex.execute(plan, now=NOW)  # same plan_id
    assert second.replayed is True
    assert second.trade_id == first.trade_id
    assert len(store.recent_trades(10)) == 1  # no double-fill
    assert store.account_state(now=NOW).positions == {"BTC": pytest.approx(10.0)}


def test_paper_trade_full_path_happy(store):
    r = paper_trade("buy 1 BTC limit 10", store, V1_CAPS, now=NOW)
    assert r["ok"] and r["status"] == "filled"
    assert r["notional"] == 10.0
    assert r["caps_after"]["total_remaining"] == pytest.approx(80.0)


def test_paper_trade_market_uses_resolved_price(store):
    r = paper_trade("buy 1 BTC market", store, V1_CAPS, resolved_price=12.0, now=NOW)
    assert r["status"] == "filled"
    assert r["fill_px"] == 12.0


@pytest.mark.parametrize(
    "text,resolved,stage",
    [
        ("garbage", None, "parse"),  # unparseable
        ("buy 1 DOGE limit 10", None, "risk_gate"),  # symbol not allowed
        ("buy 1 BTC limit 25", None, "risk_gate"),  # over per-order cap
        ("buy -1 BTC limit 10", None, "parse"),  # negative qty caught at parse
        ("buy 1 BTC market", None, "risk_gate"),  # market w/o resolved price
    ],
)
def test_paper_trade_rejections_do_not_mutate(store, text, resolved, stage):
    r = paper_trade(text, store, V1_CAPS, resolved_price=resolved, now=NOW)
    assert r["ok"] is False
    assert r["status"] == "rejected"
    assert r["stage"] == stage
    assert r["reason"]
    assert store.recent_trades(10) == []  # nothing written on rejection


def test_paper_trade_rejects_when_caps_unconfigured(store):
    r = paper_trade("buy 1 BTC limit 10", store, None, now=NOW)
    assert r["status"] == "rejected"
    assert store.recent_trades(10) == []


def test_period_cap_exhaustion_rejects_next_order(store):
    """24h turnover exhaustion is the binding constraint (not per-symbol/total).

    Seed 95 of 24h turnover via small round-trips — buys and sells both count as
    turnover, but net open exposure stays tiny — so the period cap, not the
    per-symbol or total-exposure cap, rejects the next order.
    """
    for i in range(19):
        side = "buy" if i % 2 == 0 else "sell"  # 10 buys, 9 sells @ 5 → 95 turnover, +5 open
        store.record_trade("paper", "BTC", side, 1.0, entry_px=5.0, outcome="open", now=NOW)
    assert store.account_state(now=NOW).rolling_24h_notional == pytest.approx(95.0)

    r = paper_trade("buy 1 BTC limit 10", store, V1_CAPS, now=NOW)  # 95 + 10 = 105 > 100
    assert r["status"] == "rejected"
    assert "max_period_notional" in r["reason"]
    assert len(store.recent_trades(50)) == 19  # nothing new written on rejection
