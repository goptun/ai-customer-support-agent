"""Refunds: the Refund Policy deciding eligibility and amount, and requesting a Refund.

A requested Refund is never issued here: it is created `pending_approval` with an Approval
Request, and the approval workflow is started on its own thread (ADR-0002). The amount
ceiling comes from the Order's records, never from the LLM or the Customer.
"""

import enum
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent import audit
from support_agent.approvals import ApprovalPause, ApprovalWorkflow
from support_agent.cancellations import prior_refunds_cents
from support_agent.models import (
    ActorType,
    ApprovalRequest,
    ApprovalStatus,
    Order,
    OrderStatus,
    Refund,
    RefundStatus,
)

NOT_DISPATCHED = frozenset({OrderStatus.PENDING_PAYMENT, OrderStatus.PAID, OrderStatus.PROCESSING})


@dataclass(frozen=True)
class RefundPolicy:
    delivery_window_days: int = 30
    late_after_days: int = 10


class RefundGround(enum.StrEnum):
    DELIVERED_RECENTLY = "delivered_recently"
    SHIPMENT_LOST = "shipment_lost"
    LATE = "late"


class RefundRefusal(enum.StrEnum):
    ORDER_CANCELLED = "order_cancelled"
    NOT_DISPATCHED = "not_dispatched"  # a Cancellation is the way out
    DELIVERY_WINDOW_EXPIRED = "delivery_window_expired"
    NOT_LATE_ENOUGH = "not_late_enough"
    NOTHING_LEFT = "nothing_left"  # prior Refunds already add up to the total paid
    CARRIER_UNAVAILABLE = "carrier_unavailable"  # only the carrier could tell if it was lost


@dataclass(frozen=True)
class RefundFacts:
    """The Order as the Refund Policy sees it."""

    status: OrderStatus
    total_paid_cents: int
    prior_refunds_cents: int  # every prior Refund of the Order except rejected ones
    delivered_on: date | None
    promised_on: date | None
    shipment_lost: bool | None  # None: the carrier could not be reached


@dataclass(frozen=True)
class RefundPlan:
    ground: RefundGround
    max_cents: int
    amount_cents: int
    capped: bool  # the Customer asked for more than max_cents


def _ground(facts: RefundFacts, today: date, policy: RefundPolicy) -> RefundGround | RefundRefusal:
    if facts.status is OrderStatus.CANCELLED:
        return RefundRefusal.ORDER_CANCELLED
    if facts.status in NOT_DISPATCHED:
        return RefundRefusal.NOT_DISPATCHED
    if facts.delivered_on is not None:
        if (today - facts.delivered_on).days <= policy.delivery_window_days:
            return RefundGround.DELIVERED_RECENTLY
        return RefundRefusal.DELIVERY_WINDOW_EXPIRED
    if facts.shipment_lost:
        return RefundGround.SHIPMENT_LOST
    if facts.promised_on is not None and (today - facts.promised_on).days > policy.late_after_days:
        return RefundGround.LATE
    if facts.shipment_lost is None:
        return RefundRefusal.CARRIER_UNAVAILABLE
    return RefundRefusal.NOT_LATE_ENOUGH


def decide_refund(
    facts: RefundFacts, requested_cents: int | None, today: date, policy: RefundPolicy
) -> RefundPlan | RefundRefusal:
    """Whether the Order is eligible for a Refund, and for how much.

    The amount defaults to the maximum; a lower one is kept, a higher one is capped.
    """
    ground = _ground(facts, today, policy)
    if isinstance(ground, RefundRefusal):
        return ground
    max_cents = facts.total_paid_cents - facts.prior_refunds_cents
    if max_cents <= 0:
        return RefundRefusal.NOTHING_LEFT
    if requested_cents is None:
        return RefundPlan(ground=ground, max_cents=max_cents, amount_cents=max_cents, capped=False)
    return RefundPlan(
        ground=ground,
        max_cents=max_cents,
        amount_cents=min(requested_cents, max_cents),
        capped=requested_cents > max_cents,
    )


class RefundRequested(BaseModel):
    """A Refund awaiting an Attendant's approval."""

    kind: Literal["refund_requested"] = "refund_requested"
    order_id: str
    refund_id: int
    approval_request_id: uuid.UUID
    amount_cents: int
    max_cents: int
    requested_cents: int | None
    capped: bool
    # The Order already had a Refund awaiting approval: this is that one, not a new one.
    already_pending: bool = False


class RefundRecord(BaseModel):
    refund_id: int
    order_id: str
    amount_cents: int
    status: RefundStatus
    created_at: datetime


@dataclass(frozen=True)
class RefundRequest:
    order_id: str
    customer_id: str
    conversation_id: uuid.UUID
    requested_cents: int | None
    reason: str
    shipment_lost: bool | None  # as the carrier reports it; None if it couldn't be reached


def idempotency_key(approval_request_id: uuid.UUID) -> str:
    return f"refund:{approval_request_id}"


class Refunds:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        policy: RefundPolicy,
        workflow: ApprovalWorkflow,
    ) -> None:
        self._sessions = sessions
        self.policy = policy
        self._workflow = workflow

    async def request(self, request: RefundRequest, today: date) -> RefundRequested | RefundRefusal:
        """Create a Refund awaiting approval and start its approval workflow; issues nothing.

        A request for an Order with a Refund already awaiting approval returns that one.
        """
        async with self._sessions.begin() as session:
            # The row lock serialises concurrent requests: the second one sees the first.
            order = await session.scalar(
                select(Order).where(Order.id == request.order_id).with_for_update()
            )
            assert order is not None  # the caller checked it exists and is the Customer's
            existing = await session.scalar(
                select(ApprovalRequest)
                .join(Refund, Refund.id == ApprovalRequest.refund_id)
                .where(
                    Refund.order_id == request.order_id,
                    Refund.status == RefundStatus.PENDING_APPROVAL,
                )
            )
            if existing is not None:
                result = await _requested(session, existing, already_pending=True)
            else:
                decision = decide_refund(
                    RefundFacts(
                        status=order.status,
                        total_paid_cents=order.total_paid_cents,
                        prior_refunds_cents=await prior_refunds_cents(session, order.id),
                        delivered_on=order.delivered_at.date() if order.delivered_at else None,
                        promised_on=order.promised_delivery_date,
                        shipment_lost=request.shipment_lost,
                    ),
                    request.requested_cents,
                    today,
                    self.policy,
                )
                if isinstance(decision, RefundRefusal):
                    return decision
                result = await _create(session, request, decision)
        # After commit: the workflow reads a committed Approval Request. Starting is
        # idempotent, so a repeated request also starts one a crash left unstarted.
        await self._workflow.start(
            ApprovalPause(
                approval_request_id=result.approval_request_id,
                refund_id=result.refund_id,
                order_id=result.order_id,
                amount_cents=result.amount_cents,
            )
        )
        return result

    async def of_customer(self, customer_id: str, order_id: str | None) -> list[RefundRecord]:
        """The Customer's Refunds, newest first, optionally only one Order's."""
        query = (
            select(Refund)
            .join(Order, Order.id == Refund.order_id)
            .where(Order.customer_id == customer_id)
            .order_by(Refund.created_at.desc(), Refund.id.desc())
        )
        if order_id is not None:
            query = query.where(Refund.order_id == order_id)
        async with self._sessions() as session:
            refunds = (await session.scalars(query)).all()
        return [
            RefundRecord(
                refund_id=refund.id,
                order_id=refund.order_id,
                amount_cents=refund.amount_cents,
                status=refund.status,
                created_at=refund.created_at,
            )
            for refund in refunds
        ]


async def _create(
    session: AsyncSession, request: RefundRequest, plan: RefundPlan
) -> RefundRequested:
    approval_request_id = uuid.uuid4()
    refund = Refund(
        idempotency_key=idempotency_key(approval_request_id),
        order_id=request.order_id,
        amount_cents=plan.amount_cents,
        status=RefundStatus.PENDING_APPROVAL,
        reason=request.reason,
    )
    session.add(refund)
    await session.flush()
    approval = ApprovalRequest(
        id=approval_request_id,
        refund_id=refund.id,
        order_id=request.order_id,
        customer_id=request.customer_id,
        conversation_id=request.conversation_id,
        status=ApprovalStatus.PENDING,
        policy_result={**asdict(plan), "requested_cents": request.requested_cents},
    )
    session.add(approval)
    await session.flush()
    result = await _requested(session, approval, already_pending=False)
    audit.record(
        session,
        actor_type=ActorType.CUSTOMER,
        actor_id=request.customer_id,
        action="create_refund",
        inputs={
            "order_id": request.order_id,
            "requested_cents": request.requested_cents,
            "reason": request.reason,
            "shipment_lost": request.shipment_lost,
        },
        result=result.model_dump(mode="json"),
    )
    return result


async def _requested(
    session: AsyncSession, approval: ApprovalRequest, *, already_pending: bool
) -> RefundRequested:
    refund = await session.get(Refund, approval.refund_id)
    assert refund is not None
    policy = approval.policy_result
    return RefundRequested(
        order_id=refund.order_id,
        refund_id=refund.id,
        approval_request_id=approval.id,
        amount_cents=refund.amount_cents,
        max_cents=policy["max_cents"],
        requested_cents=policy["requested_cents"],
        capped=policy["capped"],
        already_pending=already_pending,
    )
