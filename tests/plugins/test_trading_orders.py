"""Tests for the order parser + planner (orders.py)."""

from __future__ import annotations

import pytest

from plugins.trading.orders import (
    OrderParseError,
    OrderPlan,
    RiskRejected,
    parse_order,
    plan_order,
)
from plugins.trading.risk_gate import AccountState, Caps

V1_CAPS = Caps(20.0, 40.0, 100.0, ("BTC", "ETH"), 90.0)
EMPTY = AccountState()


def test_parse_limit_order():
    intent = parse_order("buy 0.01 BTC limit 60000")
    assert intent.side == "buy"
    assert intent.qty == 0.01
    assert intent.symbol == "BTC"
    assert intent.order_type == "limit"
    assert intent.price == 60000.0


def test_parse_market_order_has_no_price():
    intent = parse_order("sell 0.5 eth market")
    assert intent.side == "sell"
    assert intent.symbol == "ETH"  # upcased
    assert intent.order_type == "market"
    assert intent.price == 0.0  # resolved later


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty
        "buy 0.01 BTC",  # missing type
        "hodl 0.01 BTC limit 60000",  # bad side
        "buy nope BTC limit 60000",  # non-numeric qty
        "buy 0 BTC limit 60000",  # zero qty
        "buy -1 BTC limit 60000",  # negative qty
        "buy inf BTC limit 60000",  # non-finite qty
        "buy 1 BTC swap 60000",  # bad type
        "buy 1 BTC limit",  # limit without price
        "buy 1 BTC limit notnum",  # non-numeric price
        "buy 1 BTC limit 0",  # non-positive price
        "buy 1 BTC limit -5",  # negative price
        "buy 1 BTC market 60000",  # market with stray price
        "buy 1 BT-C limit 5",  # non-alphanumeric symbol
    ],
)
def test_parse_rejects_malformed(text):
    with pytest.raises(OrderParseError):
        parse_order(text)


def test_plan_limit_order_accepted():
    intent = parse_order("buy 1 BTC limit 10")  # notional 10
    plan = plan_order(intent, EMPTY, V1_CAPS)
    assert isinstance(plan, OrderPlan)
    assert plan.est_notional == 10.0
    assert plan.intent.price == 10.0
    assert plan.caps_after["total_remaining"] == pytest.approx(80.0)
    assert plan.plan_id and plan.created_at


def test_plan_ids_are_unique():
    intent = parse_order("buy 1 BTC limit 10")
    p1 = plan_order(intent, EMPTY, V1_CAPS)
    p2 = plan_order(intent, EMPTY, V1_CAPS)
    assert p1.plan_id != p2.plan_id


def test_plan_market_requires_resolved_price():
    intent = parse_order("buy 1 BTC market")
    with pytest.raises(RiskRejected):
        plan_order(intent, EMPTY, V1_CAPS)  # no resolved_price
    with pytest.raises(RiskRejected):
        plan_order(intent, EMPTY, V1_CAPS, resolved_price=0.0)  # non-positive


def test_plan_market_uses_resolved_price():
    intent = parse_order("buy 1 BTC market")
    plan = plan_order(intent, EMPTY, V1_CAPS, resolved_price=15.0)
    assert plan.intent.price == 15.0
    assert plan.est_notional == 15.0


def test_plan_propagates_gate_rejection_reason():
    over_cap = parse_order("buy 1 BTC limit 25")  # 25 > max_order_notional 20
    with pytest.raises(RiskRejected) as ei:
        plan_order(over_cap, EMPTY, V1_CAPS)
    assert "max_order_notional" in ei.value.reason

    bad_symbol = parse_order("buy 1 DOGE limit 10")
    with pytest.raises(RiskRejected) as ei2:
        plan_order(bad_symbol, EMPTY, V1_CAPS)
    assert "allowlist" in ei2.value.reason


def test_plan_rejects_when_caps_unconfigured():
    intent = parse_order("buy 1 BTC limit 10")
    with pytest.raises(RiskRejected):
        plan_order(intent, EMPTY, None)
