"""The Cancellation rule as a pure function: whether an Order can be Cancelled, and its Refund."""

import pytest

from support_agent.cancellations import CancellationPlan, CancellationRefusal, decide_cancellation
from support_agent.models import OrderStatus


@pytest.mark.parametrize(
    ("status", "total_paid_cents", "prior_refunds_cents", "expected"),
    [
        pytest.param(
            OrderStatus.PENDING_PAYMENT, 0, 0, CancellationPlan(refund_cents=0), id="unpaid"
        ),
        pytest.param(
            OrderStatus.PAID, 39900, 0, CancellationPlan(refund_cents=39900), id="paid_full"
        ),
        pytest.param(
            OrderStatus.PROCESSING,
            11980,
            0,
            CancellationPlan(refund_cents=11980),
            id="processing_full",
        ),
        pytest.param(
            OrderStatus.PAID,
            39900,
            9900,
            CancellationPlan(refund_cents=30000),
            id="paid_minus_prior_refunds",
        ),
        pytest.param(
            OrderStatus.PAID,
            39900,
            39900,
            CancellationPlan(refund_cents=0),
            id="paid_already_refunded",
        ),
        pytest.param(
            OrderStatus.SHIPPED, 18900, 0, CancellationRefusal.ALREADY_DISPATCHED, id="shipped"
        ),
        pytest.param(
            OrderStatus.DELIVERED,
            24990,
            0,
            CancellationRefusal.ALREADY_DISPATCHED,
            id="delivered",
        ),
        pytest.param(
            OrderStatus.CANCELLED,
            8990,
            0,
            CancellationRefusal.ALREADY_CANCELLED,
            id="cancelled",
        ),
    ],
)
def test_cancellation_rule(
    status: OrderStatus,
    total_paid_cents: int,
    prior_refunds_cents: int,
    expected: CancellationPlan | CancellationRefusal,
) -> None:
    assert decide_cancellation(status, total_paid_cents, prior_refunds_cents) == expected
