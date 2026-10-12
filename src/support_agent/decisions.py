"""Attendant decisions on Approval Requests, and relaying them to the Customer.

Deciding records the decision, the Refund's new status and the audit entry together with the
check that the request is still pending, so two Attendants can't both decide it. Then the paused
approval workflow is resumed and settles it (ADR-0002): a decision is relayed once to the
Conversation the Refund was asked in, and shown to the Customer once, on their next message,
when they poll, or at the start of a new Conversation.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel
from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent import audit
from support_agent.approvals import (
    ApprovalDecision,
    ApprovalPause,
    ApprovalWorkflow,
    build_approval_graph,
)
from support_agent.conversations import Conversations
from support_agent.models import (
    ActorType,
    ApprovalRequest,
    ApprovalStatus,
    Message,
    MessageRole,
    Refund,
    RefundStatus,
)
from support_agent.replies import render_refund_decision


class ApprovalRequestNotFound(Exception):
    """No Approval Request has that id."""


class AlreadyDecided(Exception):
    """The Approval Request was decided before; nothing was changed."""


class ApprovalRequestView(BaseModel):
    """An Approval Request as the Attendant sees it."""

    approval_request_id: uuid.UUID
    status: ApprovalStatus
    order_id: str
    customer_id: str
    conversation_id: uuid.UUID  # the one the Refund was asked in, where the decision is relayed
    refund_id: int
    amount_cents: int
    refund_reason: str  # the Customer's
    refund_status: RefundStatus
    policy_result: dict[str, Any]
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None


class Decisions:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        conversations: Conversations,
        checkpointer: BaseCheckpointSaver[Any],
    ) -> None:
        self._sessions = sessions
        self._conversations = conversations
        self.workflow = ApprovalWorkflow(build_approval_graph(checkpointer, self.settle))

    async def in_status(
        self, status: ApprovalStatus, customer_id: str | None = None
    ) -> list[ApprovalRequestView]:
        """Approval Requests in that status, oldest first; only that Customer's, if given."""
        query = _with_refund().where(ApprovalRequest.status == status)
        if customer_id is not None:
            query = query.where(ApprovalRequest.customer_id == customer_id)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    query.order_by(ApprovalRequest.created_at, ApprovalRequest.id)
                )
            ).all()
        return [_view(approval, refund) for approval, refund in rows]

    async def decide(
        self,
        approval_request_id: uuid.UUID,
        decision: ApprovalDecision,
        customer_id: str | None = None,
    ) -> ApprovalRequestView:
        """Record the decision and resume the paused workflow, which settles it.

        Given a Customer, another Customer's request is as good as missing.
        """
        async with self._sessions.begin() as session:
            # The row lock serialises concurrent decisions: the second one sees the first.
            approval = await session.get(ApprovalRequest, approval_request_id, with_for_update=True)
            if approval is None or customer_id not in (None, approval.customer_id):
                raise ApprovalRequestNotFound
            if approval.status is not ApprovalStatus.PENDING:
                raise AlreadyDecided
            refund = await session.get(Refund, approval.refund_id, with_for_update=True)
            assert refund is not None  # every Approval Request has its Refund (FK)
            approval.status = decision.status
            approval.decided_by = decision.attendant_id
            approval.decided_at = datetime.now(UTC)
            approval.decision_reason = decision.reason
            refund.status = decision.refund_status
            audit.record(
                session,
                actor_type=ActorType.ATTENDANT,
                actor_id=decision.attendant_id,
                action="decide_approval_request",
                inputs={
                    "approval_request_id": str(approval.id),
                    "order_id": approval.order_id,
                    "approved": decision.approved,
                    "reason": decision.reason,
                },
                result={"status": approval.status.value, "refund_id": refund.id},
            )
        # After commit: settling reads the committed decision.
        await self._resume(approval, refund)
        async with self._sessions() as session:
            approval = await session.get(ApprovalRequest, approval_request_id)
            refund = await session.get(Refund, approval.refund_id) if approval else None
        assert approval is not None and refund is not None
        return _view(approval, refund)

    async def resume_unsettled(self) -> None:
        """Resume the workflows of decisions a crash left unsettled; run at startup."""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    _with_refund().where(
                        ApprovalRequest.status != ApprovalStatus.PENDING,
                        ApprovalRequest.relayed_at.is_(None),
                    )
                )
            ).all()
        for approval, refund in rows:
            await self._resume(approval, refund)

    async def settle(self, approval_request_id: uuid.UUID) -> None:
        """Issue the approved Refund and relay the decision to the Conversation, once."""
        async with self._sessions.begin() as session:
            approval = await session.get(ApprovalRequest, approval_request_id, with_for_update=True)
            assert approval is not None  # settled only after its decision committed
            if approval.relayed_at is not None:
                return
            refund = await session.get(Refund, approval.refund_id, with_for_update=True)
            assert refund is not None
            if refund.status is RefundStatus.APPROVED:
                refund.status = RefundStatus.ISSUED
                audit.record(
                    session,
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action="issue_refund",
                    inputs={
                        "approval_request_id": str(approval.id),
                        "order_id": refund.order_id,
                        "refund_id": refund.id,
                    },
                    result={"status": refund.status.value, "amount_cents": refund.amount_cents},
                )
            _relay(session, approval.conversation_id, approval, refund)
            approval.relayed_at = datetime.now(UTC)
        await self._conversations.forget(approval.conversation_id)

    async def surface_unseen(self, customer_id: str, conversation_id: uuid.UUID) -> list[str]:
        """The Customer's decisions not shown yet, oldest first, now marked seen.

        Those relayed to another Conversation are appended to this one too.
        """
        async with self._sessions.begin() as session:
            rows = (
                await session.execute(
                    _with_refund()
                    .where(
                        ApprovalRequest.customer_id == customer_id,
                        ApprovalRequest.relayed_at.is_not(None),
                        ApprovalRequest.seen_at.is_(None),
                    )
                    .order_by(ApprovalRequest.decided_at, ApprovalRequest.id)
                    .with_for_update(of=ApprovalRequest, skip_locked=True)
                )
            ).all()
            notices = [_decision_text(approval, refund) for approval, refund in rows]
            copied = [(a, r) for a, r in rows if a.conversation_id != conversation_id]
            for approval, refund in copied:
                _relay(session, conversation_id, approval, refund)
            for approval, _ in rows:
                approval.seen_at = datetime.now(UTC)
        if copied:
            await self._conversations.forget(conversation_id)
        return notices

    async def mark_seen(self, conversation_id: uuid.UUID) -> None:
        """The Customer saw the decisions relayed to the Conversation (e.g. by polling it)."""
        async with self._sessions.begin() as session:
            await session.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.conversation_id == conversation_id,
                    ApprovalRequest.relayed_at.is_not(None),
                    ApprovalRequest.seen_at.is_(None),
                )
                .values(seen_at=datetime.now(UTC))
            )

    async def _resume(self, approval: ApprovalRequest, refund: Refund) -> None:
        assert approval.decided_by is not None  # resumed only once decided
        # Starting is idempotent: it also starts a workflow a crash left unstarted.
        await self.workflow.start(
            ApprovalPause(
                approval_request_id=approval.id,
                refund_id=refund.id,
                order_id=refund.order_id,
                amount_cents=refund.amount_cents,
            )
        )
        await self.workflow.resume(
            approval.id,
            ApprovalDecision(
                approved=approval.status is ApprovalStatus.APPROVED,
                attendant_id=approval.decided_by,
                reason=approval.decision_reason,
            ),
        )


def _with_refund() -> Select[ApprovalRequest, Refund]:
    return select(ApprovalRequest, Refund).join(Refund, Refund.id == ApprovalRequest.refund_id)


def _relay(
    session: AsyncSession, conversation_id: uuid.UUID, approval: ApprovalRequest, refund: Refund
) -> None:
    """Append the decision to the Conversation as an agent message."""
    session.add(
        Message(
            conversation_id=conversation_id,
            role=MessageRole.AGENT,
            content=_decision_text(approval, refund),
        )
    )


def _decision_text(approval: ApprovalRequest, refund: Refund) -> str:
    return render_refund_decision(
        refund.order_id,
        refund.id,
        refund.amount_cents,
        approved=approval.status is ApprovalStatus.APPROVED,
        reason=approval.decision_reason,
    )


def _view(approval: ApprovalRequest, refund: Refund) -> ApprovalRequestView:
    return ApprovalRequestView(
        approval_request_id=approval.id,
        status=approval.status,
        order_id=approval.order_id,
        customer_id=approval.customer_id,
        conversation_id=approval.conversation_id,
        refund_id=refund.id,
        amount_cents=refund.amount_cents,
        refund_reason=refund.reason,
        refund_status=refund.status,
        policy_result=approval.policy_result,
        created_at=approval.created_at,
        decided_by=approval.decided_by,
        decided_at=approval.decided_at,
        decision_reason=approval.decision_reason,
    )
