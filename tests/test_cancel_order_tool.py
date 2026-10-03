"""Cancellation tools against real Postgres: the rule, atomicity, idempotency and audit."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.cancellations import CancellationResult
from support_agent.db import session_factory
from support_agent.models import (
    AuditLogEntry,
    Cancellation,
    Order,
    OrderStatus,
    Refund,
    RefundStatus,
)
from support_agent.seed import ANCHOR
from support_agent.tools import (
    CancellationPreview,
    ErrorCode,
    ToolError,
    cancel_order,
    preview_cancellation,
)

from .test_ticket_tool import deps

ANA = "cus_ana"


async def new_order(
    engine: AsyncEngine,
    status: OrderStatus = OrderStatus.PAID,
    total_paid_cents: int = 39900,
    customer_id: str = ANA,
) -> str:
    """An Order of its own, placed long ago so it never shows up among recent Orders."""
    order_id = f"ORD-9{uuid.uuid4().int % 10**10:010d}"
    async with session_factory(engine).begin() as session:
        session.add(
            Order(
                id=order_id,
                customer_id=customer_id,
                status=status,
                items=[{"sku": "SKU-1", "name": "Caneca", "quantity": 1}],
                total_paid_cents=total_paid_cents,
                placed_at=ANCHOR - timedelta(days=400),
                promised_delivery_date=None,
                delivered_at=None,
            )
        )
    return order_id


async def order_state(
    engine: AsyncEngine, order_id: str
) -> tuple[OrderStatus, list[Cancellation], list[Refund]]:
    async with session_factory(engine)() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        cancellations = (
            await session.scalars(select(Cancellation).where(Cancellation.order_id == order_id))
        ).all()
        refunds = (await session.scalars(select(Refund).where(Refund.order_id == order_id))).all()
        return order.status, list(cancellations), list(refunds)


async def audit_of(engine: AsyncEngine, order_id: str) -> list[AuditLogEntry]:
    async with session_factory(engine)() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLogEntry)
                    .where(AuditLogEntry.inputs["order_id"].as_string() == order_id)
                    .order_by(AuditLogEntry.id)
                )
            ).all()
        )


async def test_preview_states_the_full_refund_of_a_paid_order(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PROCESSING, 11980)

    result = await preview_cancellation(deps(engine), ANA, {"order_id": order_id})

    assert result == CancellationPreview(
        order_id=order_id, status=OrderStatus.PROCESSING, refund_cents=11980
    )
    assert (await order_state(engine, order_id))[0] is OrderStatus.PROCESSING


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (OrderStatus.SHIPPED, ErrorCode.ALREADY_DISPATCHED),
        (OrderStatus.DELIVERED, ErrorCode.ALREADY_DISPATCHED),
        (OrderStatus.CANCELLED, ErrorCode.ALREADY_CANCELLED),
    ],
)
async def test_preview_refuses_an_order_that_cannot_be_cancelled(
    engine: AsyncEngine, status: OrderStatus, code: ErrorCode
) -> None:
    order_id = await new_order(engine, status)

    result = await preview_cancellation(deps(engine), ANA, {"order_id": order_id})

    assert result == ToolError(code=code, order_id=order_id)


async def test_paid_order_is_cancelled_with_its_full_refund_issued(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 39900)

    result = await cancel_order(deps(engine), ANA, {"order_id": order_id})

    assert isinstance(result, CancellationResult)
    status, [cancellation], [refund] = await order_state(engine, order_id)
    assert status is OrderStatus.CANCELLED
    assert cancellation.idempotency_key == f"cancel:{order_id}"
    assert cancellation.refund_id == refund.id
    assert refund.status is RefundStatus.ISSUED
    assert refund.amount_cents == 39900
    assert result.refund is not None
    assert result.refund.refund_id == refund.id
    assert result.refund.amount_cents == 39900


async def test_unpaid_order_is_cancelled_without_a_refund(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PENDING_PAYMENT, 0)

    result = await cancel_order(deps(engine), ANA, {"order_id": order_id})

    assert result == CancellationResult(order_id=order_id, refund=None)
    status, [_], refunds = await order_state(engine, order_id)
    assert status is OrderStatus.CANCELLED
    assert refunds == []


async def test_cancellation_and_its_refund_are_audited(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 39900)

    result = await cancel_order(deps(engine), ANA, {"order_id": order_id})

    assert isinstance(result, CancellationResult) and result.refund is not None
    cancelled, refunded = await audit_of(engine, order_id)
    assert (cancelled.action, cancelled.actor_type, cancelled.actor_id) == (
        "cancel_order",
        "customer",
        ANA,
    )
    assert cancelled.inputs["previous_status"] == "paid"
    assert cancelled.result == result.model_dump(mode="json")
    assert (refunded.action, refunded.actor_type, refunded.actor_id) == (
        "issue_refund",
        "system",
        None,
    )
    assert refunded.inputs["amount_cents"] == 39900
    assert refunded.result == result.refund.model_dump(mode="json")


async def test_repeated_confirmation_has_no_extra_effect(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    tool_deps = deps(engine)

    first = await cancel_order(tool_deps, ANA, {"order_id": order_id})
    again = await cancel_order(tool_deps, ANA, {"order_id": order_id})

    assert isinstance(first, CancellationResult)
    assert again == first
    _, cancellations, refunds = await order_state(engine, order_id)
    assert len(cancellations) == 1 and len(refunds) == 1
    assert len(await audit_of(engine, order_id)) == 2


async def test_concurrent_confirmations_cancel_once(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    tool_deps = deps(engine)

    results = await asyncio.gather(
        *(cancel_order(tool_deps, ANA, {"order_id": order_id}) for _ in range(3))
    )

    assert all(isinstance(r, CancellationResult) for r in results)
    assert results.count(results[0]) == 3
    _, cancellations, refunds = await order_state(engine, order_id)
    assert len(cancellations) == 1 and len(refunds) == 1


async def test_dispatched_order_is_not_cancelled(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.SHIPPED, 18900)

    result = await cancel_order(deps(engine), ANA, {"order_id": order_id})

    assert result == ToolError(code=ErrorCode.ALREADY_DISPATCHED, order_id=order_id)
    assert await order_state(engine, order_id) == (OrderStatus.SHIPPED, [], [])
    assert await audit_of(engine, order_id) == []


async def test_order_of_another_customer_is_refused(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 39900, customer_id="cus_bruno")

    result = await cancel_order(deps(engine), ANA, {"order_id": order_id})

    assert result == ToolError(code=ErrorCode.NOT_OWNED, order_id=order_id)
    assert await order_state(engine, order_id) == (OrderStatus.PAID, [], [])


async def test_unknown_order_and_invalid_argument_are_structured_errors(
    engine: AsyncEngine,
) -> None:
    unknown = await cancel_order(deps(engine), ANA, {"order_id": "ORD-999999999999"})
    invalid = await cancel_order(deps(engine), ANA, {"order_id": "abc'; DROP TABLE"})

    assert unknown == ToolError(code=ErrorCode.NOT_FOUND, order_id="ORD-999999999999")
    assert invalid == ToolError(code=ErrorCode.INVALID_ARGUMENT)


async def test_a_failed_refund_leaves_the_order_untouched(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    # A stray Refund holding the Cancellation's key makes issuing the Refund fail.
    async with session_factory(engine).begin() as session:
        session.add(
            Refund(
                idempotency_key=f"cancel:{order_id}",
                order_id=await new_order(engine),
                amount_cents=100,
                status=RefundStatus.ISSUED,
                reason="stray",
            )
        )

    with pytest.raises(IntegrityError):
        await cancel_order(deps(engine), ANA, {"order_id": order_id})

    assert await order_state(engine, order_id) == (OrderStatus.PAID, [], [])
    assert await audit_of(engine, order_id) == []
