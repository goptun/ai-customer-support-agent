"""Tickets: idempotent hand-offs of a Conversation to an Attendant, audited on creation."""

import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent import audit
from support_agent.models import ActorType, Conversation, Ticket, TicketPriority, TicketReason


class TicketResult(BaseModel):
    kind: Literal["ticket"] = "ticket"
    ticket_id: int
    reason: TicketReason
    priority: TicketPriority
    order_id: str | None


def idempotency_key(conversation_id: uuid.UUID, reason: TicketReason) -> str:
    return f"ticket:{conversation_id}:{reason.value}"


@dataclass(frozen=True)
class NewTicket:
    conversation_id: uuid.UUID
    customer_id: str
    order_id: str | None
    reason: TicketReason
    priority: TicketPriority
    summary: str


class Tickets:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def conversation_owner(self, conversation_id: uuid.UUID) -> str | None:
        async with self._sessions() as session:
            conversation = await session.get(Conversation, conversation_id)
        return None if conversation is None else conversation.customer_id

    async def open(
        self,
        new: NewTicket,
        *,
        actor_type: ActorType,
        actor_id: str | None,
    ) -> TicketResult:
        """Open the Ticket, or return the one already opened under the same key.

        The audit entry is written only when the Ticket is actually created.
        """
        key = idempotency_key(new.conversation_id, new.reason)
        async with self._sessions.begin() as session:
            ticket_id = await session.scalar(
                insert(Ticket)
                .values(
                    idempotency_key=key,
                    conversation_id=new.conversation_id,
                    customer_id=new.customer_id,
                    order_id=new.order_id,
                    reason=new.reason,
                    priority=new.priority,
                    summary=new.summary,
                )
                .on_conflict_do_nothing(index_elements=[Ticket.idempotency_key])
                .returning(Ticket.id)
            )
            ticket = await session.scalar(select(Ticket).where(Ticket.idempotency_key == key))
            assert ticket is not None
            result = TicketResult(
                ticket_id=ticket.id,
                reason=ticket.reason,
                priority=ticket.priority,
                order_id=ticket.order_id,
            )
            if ticket_id is not None:
                audit.record(
                    session,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="create_support_ticket",
                    inputs={
                        "conversation_id": str(new.conversation_id),
                        "customer_id": new.customer_id,
                        "order_id": new.order_id,
                        "reason": new.reason.value,
                        "priority": new.priority.value,
                        "summary": new.summary,
                    },
                    result=result.model_dump(mode="json"),
                )
        return result
