"""Cancellations: the rule deciding whether an Order can be Cancelled, and applying it.

A paid Order's Cancellation issues its full Refund in the same transaction, without an
Approval Request (ADR-0003). Both are idempotent under `cancel:{order_id}` and audited.
"""

import enum
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent import audit
from support_agent.models import (
    NOT_DISPATCHED,
    ActorType,
    Cancellation,
    Order,
    OrderStatus,
    Refund,
    RefundStatus,
)
from support_agent.refunds import prior_refunds_cents

CANCELLABLE = NOT_DISPATCHED
PAID_STATUSES = frozenset({OrderStatus.PAID, OrderStatus.PROCESSING})
REFUND_REASON = "Cancelamento do pedido antes do envio"


class CancellationRefusal(enum.StrEnum):
    ALREADY_DISPATCHED = "already_dispatched"
    ALREADY_CANCELLED = "already_cancelled"
    # The Refund is no longer the one the Customer confirmed (e.g. the Order was paid since).
    REFUND_CHANGED = "refund_changed"


@dataclass(frozen=True)
class CancellationPlan:
    refund_cents: int  # 0: nothing was paid, or it was all refunded already


def decide_cancellation(
    status: OrderStatus, total_paid_cents: int, prior_refunds_cents: int
) -> CancellationPlan | CancellationRefusal:
    """Whether a whole Order can be Cancelled now, and the full Refund that implies."""
    if status is OrderStatus.CANCELLED:
        return CancellationRefusal.ALREADY_CANCELLED
    if status not in CANCELLABLE:
        return CancellationRefusal.ALREADY_DISPATCHED
    if status not in PAID_STATUSES:
        return CancellationPlan(refund_cents=0)
    return CancellationPlan(refund_cents=max(0, total_paid_cents - prior_refunds_cents))


def idempotency_key(order_id: str) -> str:
    return f"cancel:{order_id}"


class RefundSummary(BaseModel):
    refund_id: int
    amount_cents: int
    status: RefundStatus


class CancellationResult(BaseModel):
    kind: Literal["cancellation"] = "cancellation"
    order_id: str
    refund: RefundSummary | None


class Cancellations:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def preview(self, order: Order) -> CancellationPlan | CancellationRefusal:
        """What cancelling the Order now would do; changes nothing."""
        async with self._sessions() as session:
            prior = await prior_refunds_cents(session, order.id)
        return decide_cancellation(order.status, order.total_paid_cents, prior)

    async def cancel(
        self, order_id: str, customer_id: str, expected_refund_cents: int
    ) -> CancellationResult | CancellationRefusal:
        """Cancel the Order (and issue its Refund), or return the Cancellation already made.

        Audit entries are written only when the Cancellation is actually made.
        """
        key = idempotency_key(order_id)
        async with self._sessions.begin() as session:
            # The row lock serialises concurrent confirmations: the second one sees the first.
            order = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            assert order is not None  # the caller checked it exists and is the Customer's
            existing = await session.scalar(
                select(Cancellation).where(Cancellation.idempotency_key == key)
            )
            if existing is not None:
                return await _cancellation_result(session, existing)

            decision = decide_cancellation(
                order.status, order.total_paid_cents, await prior_refunds_cents(session, order_id)
            )
            if isinstance(decision, CancellationRefusal):
                return decision
            if decision.refund_cents != expected_refund_cents:
                return CancellationRefusal.REFUND_CHANGED

            refund_id = None
            if decision.refund_cents > 0:
                refund_id = await session.scalar(
                    insert(Refund)
                    .values(
                        idempotency_key=key,
                        order_id=order_id,
                        amount_cents=decision.refund_cents,
                        status=RefundStatus.ISSUED,
                        reason=REFUND_REASON,
                    )
                    .returning(Refund.id)
                )
            previous_status = order.status
            order.status = OrderStatus.CANCELLED
            cancellation = Cancellation(
                idempotency_key=key, order_id=order_id, customer_id=customer_id, refund_id=refund_id
            )
            session.add(cancellation)
            await session.flush()
            result = await _cancellation_result(session, cancellation)

            audit.record(
                session,
                actor_type=ActorType.CUSTOMER,
                actor_id=customer_id,
                action="cancel_order",
                inputs={"order_id": order_id, "previous_status": previous_status.value},
                result=result.model_dump(mode="json"),
            )
            if result.refund is not None:
                # Follows from the Cancellation in code, not from anyone's decision (ADR-0003).
                audit.record(
                    session,
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action="issue_refund",
                    inputs={
                        "order_id": order_id,
                        "amount_cents": result.refund.amount_cents,
                        "reason": REFUND_REASON,
                        "idempotency_key": key,
                    },
                    result=result.refund.model_dump(mode="json"),
                )
        return result


async def _cancellation_result(
    session: AsyncSession, cancellation: Cancellation
) -> CancellationResult:
    refund = None
    if cancellation.refund_id is not None:
        row = await session.get(Refund, cancellation.refund_id)
        assert row is not None
        refund = RefundSummary(refund_id=row.id, amount_cents=row.amount_cents, status=row.status)
    return CancellationResult(order_id=cancellation.order_id, refund=refund)
