"""Conversations: durable record in Postgres, short-lived working state in Redis.

Redis is only a cache (ADR-0001): if the working state expires or is lost, it is rebuilt
from the messages stored in Postgres.
"""

import json
import uuid
from dataclasses import asdict, dataclass, field

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.models import Conversation, Message, MessageRole


class ConversationNotFound(Exception):
    """The Conversation does not exist or belongs to another Customer (indistinguishable)."""


@dataclass
class WorkingState:
    history: list[dict[str, str]] = field(default_factory=list)
    intent: str | None = None


class Conversations:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: Redis,
        ttl_seconds: int,
        window: int,
    ) -> None:
        self._sessions = sessions
        self._redis = redis
        self._ttl = ttl_seconds
        self._window = window

    async def open(self, customer_id: str, conversation_id: uuid.UUID | None) -> uuid.UUID:
        """Return an existing Conversation owned by the Customer, or start a new one."""
        async with self._sessions.begin() as session:
            if conversation_id is None:
                conversation = Conversation(customer_id=customer_id)
                session.add(conversation)
                await session.flush()
                return conversation.id
            found = await session.get(Conversation, conversation_id)
            if found is None or found.customer_id != customer_id:
                raise ConversationNotFound
            return found.id

    async def record(
        self,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str,
        intent: str | None = None,
    ) -> None:
        # Load (or rebuild) before inserting, so a rebuild doesn't already include this message.
        state = await self.working_state(conversation_id)
        async with self._sessions.begin() as session:
            session.add(
                Message(conversation_id=conversation_id, role=role, content=content, intent=intent)
            )
        state.history = [*state.history, {"role": role.value, "content": content}][-self._window :]
        if intent is not None:
            state.intent = intent
        await self._save(conversation_id, state)

    async def working_state(self, conversation_id: uuid.UUID) -> WorkingState:
        cached = await self._redis.get(self._key(conversation_id))
        if cached is not None:
            return WorkingState(**json.loads(cached))
        return await self._rebuild(conversation_id)

    async def _rebuild(self, conversation_id: uuid.UUID) -> WorkingState:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.id.desc())
                    .limit(self._window)
                )
            ).all()
        recent = list(reversed(rows))
        return WorkingState(
            history=[{"role": m.role.value, "content": m.content} for m in recent],
            intent=next((m.intent for m in rows if m.intent is not None), None),
        )

    async def _save(self, conversation_id: uuid.UUID, state: WorkingState) -> None:
        await self._redis.set(self._key(conversation_id), json.dumps(asdict(state)), ex=self._ttl)

    @staticmethod
    def _key(conversation_id: uuid.UUID) -> str:
        return f"conversation:{conversation_id}:state"
