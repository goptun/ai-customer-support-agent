"""The Refund Policy as a pure function: eligibility, its ground, and the amount."""

from datetime import date

import pytest

from support_agent.models import OrderStatus
from support_agent.refunds import (
    RefundFacts,
    RefundGround,
    RefundPlan,
    RefundPolicy,
    RefundRefusal,
    decide_refund,
)

TODAY = date(2026, 9, 1)
POLICY = RefundPolicy(delivery_window_days=30, late_after_days=10)


def facts(
    status: OrderStatus = OrderStatus.SHIPPED,
    *,
    total_paid_cents: int = 10000,
    prior_refunds_cents: int = 0,
    delivered_on: date | None = None,
    promised_on: date | None = date(2026, 8, 30),
    shipment_lost: bool | None = False,
) -> RefundFacts:
    return RefundFacts(
        status=status,
        total_paid_cents=total_paid_cents,
        prior_refunds_cents=prior_refunds_cents,
        delivered_on=delivered_on,
        promised_on=promised_on,
        shipment_lost=shipment_lost,
    )


def delivered(on: date, prior_refunds_cents: int = 0) -> RefundFacts:
    return facts(OrderStatus.DELIVERED, delivered_on=on, prior_refunds_cents=prior_refunds_cents)


def full(ground: RefundGround, cents: int = 10000) -> RefundPlan:
    return RefundPlan(ground=ground, max_cents=cents, amount_cents=cents, capped=False)


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        pytest.param(
            delivered(date(2026, 8, 2)),
            full(RefundGround.DELIVERED_RECENTLY),
            id="delivered_exactly_30_days_ago",
        ),
        pytest.param(
            delivered(date(2026, 8, 1)),
            RefundRefusal.DELIVERY_WINDOW_EXPIRED,
            id="delivered_31_days_ago",
        ),
        pytest.param(
            facts(shipment_lost=True), full(RefundGround.SHIPMENT_LOST), id="shipment_lost"
        ),
        pytest.param(
            facts(promised_on=date(2026, 8, 21)),
            full(RefundGround.LATE),
            id="11_days_past_promised",
        ),
        pytest.param(
            facts(promised_on=date(2026, 8, 22)),
            RefundRefusal.NOT_LATE_ENOUGH,
            id="exactly_10_days_past_promised",
        ),
        pytest.param(
            facts(promised_on=None), RefundRefusal.NOT_LATE_ENOUGH, id="in_transit_no_promise"
        ),
        pytest.param(
            facts(promised_on=date(2026, 8, 21), shipment_lost=None),
            full(RefundGround.LATE),
            id="late_needs_no_carrier",
        ),
        pytest.param(
            facts(shipment_lost=None),
            RefundRefusal.CARRIER_UNAVAILABLE,
            id="in_transit_carrier_unknown",
        ),
        pytest.param(
            facts(OrderStatus.PAID, promised_on=date(2026, 8, 1)),
            RefundRefusal.NOT_DISPATCHED,
            id="paid_not_dispatched",
        ),
        pytest.param(
            facts(OrderStatus.PENDING_PAYMENT, total_paid_cents=0),
            RefundRefusal.NOT_DISPATCHED,
            id="unpaid",
        ),
        pytest.param(
            facts(OrderStatus.CANCELLED, shipment_lost=True),
            RefundRefusal.ORDER_CANCELLED,
            id="cancelled",
        ),
        pytest.param(
            delivered(date(2026, 8, 20), prior_refunds_cents=4000),
            full(RefundGround.DELIVERED_RECENTLY, 6000),
            id="max_is_paid_minus_prior_refunds",
        ),
        pytest.param(
            delivered(date(2026, 8, 20), prior_refunds_cents=10000),
            RefundRefusal.NOTHING_LEFT,
            id="already_fully_refunded",
        ),
    ],
)
def test_eligibility_and_max(order: RefundFacts, expected: RefundPlan | RefundRefusal) -> None:
    assert decide_refund(order, None, TODAY, POLICY) == expected


@pytest.mark.parametrize(
    ("requested_cents", "amount_cents", "capped"),
    [
        pytest.param(None, 10000, False, id="defaults_to_max"),
        pytest.param(2500, 2500, False, id="lower_allowed"),
        pytest.param(10000, 10000, False, id="exactly_max"),
        pytest.param(25000, 10000, True, id="higher_capped"),
    ],
)
def test_amount(requested_cents: int | None, amount_cents: int, capped: bool) -> None:
    plan = decide_refund(facts(shipment_lost=True), requested_cents, TODAY, POLICY)

    assert plan == RefundPlan(
        ground=RefundGround.SHIPMENT_LOST,
        max_cents=10000,
        amount_cents=amount_cents,
        capped=capped,
    )


def test_thresholds_come_from_the_policy() -> None:
    lenient = RefundPolicy(delivery_window_days=60, late_after_days=1)

    assert decide_refund(delivered(date(2026, 7, 10)), None, TODAY, lenient) == full(
        RefundGround.DELIVERED_RECENTLY
    )
    assert decide_refund(facts(promised_on=date(2026, 8, 30)), None, TODAY, lenient) == full(
        RefundGround.LATE
    )
