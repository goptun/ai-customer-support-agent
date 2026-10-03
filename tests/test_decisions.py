"""Decisions against real Postgres: a decision a crash left unsettled is settled at startup."""

from collections.abc import AsyncIterator

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.approvals import ApprovalDecision
from support_agent.config import Settings
from support_agent.conversations import Conversations
from support_agent.db import session_factory
from support_agent.decisions import Decisions
from support_agent.refunds import RefundPolicy, RefundRequest, RefundRequested, Refunds
from support_agent.seed import ANCHOR

from .test_api import stored_messages
from .test_cancel_order_tool import audit_of
from .test_refund_tools import dispatched_order, refunds_of
from .test_ticket_tool import new_conversation

ANA = "cus_ana"
JULIA = "att_julia"


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = Redis.from_url(settings.redis_url)
    yield client
    await client.aclose()


async def test_a_decision_left_unsettled_by_a_crash_is_settled_at_startup(
    engine: AsyncEngine, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = session_factory(engine)
    conversations = Conversations(sessions, redis, ttl_seconds=60, window=20)
    checkpointer = InMemorySaver()  # survives the "restart", as Postgres would
    before_crash = Decisions(sessions, conversations, checkpointer)
    order_id = await dispatched_order(engine, delivered_days_ago=4)
    conversation_id = await new_conversation(engine, ANA)
    requested = await Refunds(sessions, RefundPolicy(), before_crash.workflow).request(
        RefundRequest(order_id, ANA, conversation_id, None, "chegou quebrado", None),
        ANCHOR.date(),
    )
    assert isinstance(requested, RefundRequested)

    async def crash(*_: object) -> None:
        raise ConnectionError("process died")

    monkeypatch.setattr(before_crash.workflow, "resume", crash)
    with pytest.raises(ConnectionError):
        await before_crash.decide(
            requested.approval_request_id,
            ApprovalDecision(approved=True, attendant_id=JULIA, reason=None),
        )
    [approved] = await refunds_of(engine, order_id)
    assert approved.status == "approved"

    await Decisions(sessions, conversations, checkpointer).resume_unsettled()
    await Decisions(sessions, conversations, checkpointer).resume_unsettled()

    [issued] = await refunds_of(engine, order_id)
    assert issued.status == "issued"
    relayed = [m for m in await stored_messages(engine, str(conversation_id)) if m[0] == "agent"]
    assert len(relayed) == 1 and "aprovado" in relayed[0][1]
    actions = [entry.action for entry in await audit_of(engine, order_id)]
    assert actions == ["create_refund", "decide_approval", "issue_refund"]
