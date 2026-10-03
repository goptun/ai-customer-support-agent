"""create_support_ticket against real Postgres: idempotency, ownership and the audit log."""

import uuid

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.db import session_factory
from support_agent.knowledge import KnowledgeBase
from support_agent.logistics import LogisticsClient
from support_agent.models import AuditLogEntry, Conversation, Ticket
from support_agent.orders import Orders
from support_agent.seed import ANCHOR
from support_agent.tickets import TicketResult, Tickets
from support_agent.tools import (
    ErrorCode,
    ToolDeps,
    ToolError,
    create_support_ticket,
)

from .conftest import KNOWLEDGE_MIN_SCORE
from .fakes import LexicalEmbedder

ANA = "cus_ana"


def deps(engine: AsyncEngine) -> ToolDeps:
    def no_carrier(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected carrier call: {request.url}")

    sessions = session_factory(engine)
    return ToolDeps(
        orders=Orders(sessions),
        logistics=LogisticsClient(
            httpx.AsyncClient(transport=httpx.MockTransport(no_carrier)), timeout_seconds=0.5
        ),
        tickets=Tickets(sessions),
        knowledge_base=KnowledgeBase(
            sessions, LexicalEmbedder(), top_k=3, min_score=KNOWLEDGE_MIN_SCORE
        ),
        today=ANCHOR.date,
    )


async def new_conversation(engine: AsyncEngine, customer_id: str = ANA) -> uuid.UUID:
    async with session_factory(engine).begin() as session:
        conversation = Conversation(customer_id=customer_id)
        session.add(conversation)
        await session.flush()
        return conversation.id


async def tickets_of(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[Ticket]:
    async with session_factory(engine)() as session:
        return list(
            (
                await session.scalars(
                    select(Ticket).where(Ticket.conversation_id == conversation_id)
                )
            ).all()
        )


async def audit_entries(engine: AsyncEngine, ticket_id: int) -> list[AuditLogEntry]:
    async with session_factory(engine)() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLogEntry).where(
                        AuditLogEntry.action == "create_support_ticket",
                        AuditLogEntry.result["ticket_id"].as_integer() == ticket_id,
                    )
                )
            ).all()
        )


def args(**overrides: object) -> dict[str, object]:
    return {
        "reason": "customer_requested",
        "priority": "normal",
        "summary": "Customer pediu atendimento humano.",
        "order_id": "ORD-1002",
        **overrides,
    }


async def test_ticket_carries_summary_focused_order_and_priority(engine: AsyncEngine) -> None:
    conversation_id = await new_conversation(engine)

    result = await create_support_ticket(deps(engine), ANA, conversation_id, args())

    assert isinstance(result, TicketResult)
    [ticket] = await tickets_of(engine, conversation_id)
    assert ticket.id == result.ticket_id
    assert ticket.customer_id == ANA
    assert ticket.order_id == "ORD-1002"
    assert ticket.priority == "normal"
    assert ticket.summary == "Customer pediu atendimento humano."
    assert ticket.idempotency_key == f"ticket:{conversation_id}:customer_requested"


async def test_repeat_for_the_same_reason_returns_the_original_ticket(
    engine: AsyncEngine,
) -> None:
    conversation_id = await new_conversation(engine)
    tool_deps = deps(engine)

    first = await create_support_ticket(tool_deps, ANA, conversation_id, args())
    again = await create_support_ticket(
        tool_deps, ANA, conversation_id, args(summary="Outro resumo", priority="high")
    )

    assert isinstance(first, TicketResult)
    assert again == first
    assert len(await tickets_of(engine, conversation_id)) == 1
    assert len(await audit_entries(engine, first.ticket_id)) == 1


async def test_a_different_reason_opens_another_ticket(engine: AsyncEngine) -> None:
    conversation_id = await new_conversation(engine)
    tool_deps = deps(engine)

    human = await create_support_ticket(tool_deps, ANA, conversation_id, args())
    failure = await create_support_ticket(
        tool_deps, ANA, conversation_id, args(reason="tool_failure", priority="high")
    )

    assert isinstance(human, TicketResult) and isinstance(failure, TicketResult)
    assert human.ticket_id != failure.ticket_id


async def test_customer_comes_from_auth_not_from_arguments(engine: AsyncEngine) -> None:
    conversation_id = await new_conversation(engine)

    result = await create_support_ticket(
        deps(engine), ANA, conversation_id, args(customer_id="cus_bruno")
    )

    assert isinstance(result, TicketResult)
    [ticket] = await tickets_of(engine, conversation_id)
    assert ticket.customer_id == ANA


async def test_order_of_another_customer_is_refused(engine: AsyncEngine) -> None:
    conversation_id = await new_conversation(engine)

    result = await create_support_ticket(
        deps(engine), ANA, conversation_id, args(order_id="ORD-2002")
    )

    assert isinstance(result, ToolError) and result.code is ErrorCode.NOT_OWNED
    assert await tickets_of(engine, conversation_id) == []


async def test_conversation_of_another_customer_is_refused(engine: AsyncEngine) -> None:
    conversation_id = await new_conversation(engine, customer_id="cus_bruno")

    result = await create_support_ticket(deps(engine), ANA, conversation_id, args(order_id=None))

    assert isinstance(result, ToolError) and result.code is ErrorCode.NOT_OWNED
    assert await tickets_of(engine, conversation_id) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"reason": "bored"},
        {"priority": "whenever"},
        {"summary": ""},
        {"order_id": "abc'; DROP TABLE"},
    ],
)
async def test_invalid_arguments_are_a_structured_error(
    engine: AsyncEngine, bad: dict[str, object]
) -> None:
    conversation_id = await new_conversation(engine)

    result = await create_support_ticket(deps(engine), ANA, conversation_id, args(**bad))

    assert isinstance(result, ToolError) and result.code is ErrorCode.INVALID_ARGUMENT


async def test_ticket_is_audited_with_actor_inputs_and_result(engine: AsyncEngine) -> None:
    conversation_id = await new_conversation(engine)

    result = await create_support_ticket(deps(engine), ANA, conversation_id, args())

    assert isinstance(result, TicketResult)
    [entry] = await audit_entries(engine, result.ticket_id)
    assert entry.actor_type == "customer"
    assert entry.actor_id == ANA
    assert entry.inputs["conversation_id"] == str(conversation_id)
    assert entry.inputs["reason"] == "customer_requested"
    assert entry.result == result.model_dump(mode="json")
    assert entry.created_at is not None


async def test_ticket_opened_by_the_system_is_audited_as_system(engine: AsyncEngine) -> None:
    conversation_id = await new_conversation(engine)

    result = await create_support_ticket(
        deps(engine), ANA, conversation_id, args(reason="tool_failure", priority="high")
    )

    assert isinstance(result, TicketResult)
    [entry] = await audit_entries(engine, result.ticket_id)
    assert entry.actor_type == "system"
    assert entry.actor_id is None


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log"]
)
async def test_audit_log_is_append_only(engine: AsyncEngine, statement: str) -> None:
    conversation_id = await new_conversation(engine)
    await create_support_ticket(deps(engine), ANA, conversation_id, args())

    async with engine.begin() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text(statement))

    async with engine.connect() as conn:
        count = await conn.scalar(select(func.count()).select_from(AuditLogEntry))
        assert count is not None and count > 0
