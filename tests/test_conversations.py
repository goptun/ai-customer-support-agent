from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.config import Settings
from support_agent.conversations import Conversations
from support_agent.db import session_factory
from support_agent.models import MessageRole


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = Redis.from_url(settings.redis_url)
    yield client
    await client.aclose()


def conversations(engine: AsyncEngine, redis: Redis, window: int = 20) -> Conversations:
    return Conversations(session_factory(engine), redis, ttl_seconds=60, window=window)


async def test_working_state_is_rebuilt_from_postgres_without_duplicates(
    engine: AsyncEngine, redis: Redis
) -> None:
    store = conversations(engine, redis)
    conversation_id = await store.open("cus_carla", None)
    await store.record(conversation_id, MessageRole.CUSTOMER, "um")
    await store.record(conversation_id, MessageRole.AGENT, "dois", intent="other")

    await redis.flushdb()
    await store.record(conversation_id, MessageRole.CUSTOMER, "três")

    state = await store.working_state(conversation_id)
    assert [m["content"] for m in state.history] == ["um", "dois", "três"]
    assert state.intent == "other"


async def test_working_state_keeps_only_the_recent_window(
    engine: AsyncEngine, redis: Redis
) -> None:
    store = conversations(engine, redis, window=2)
    conversation_id = await store.open("cus_carla", None)
    for content in ["a", "b", "c"]:
        await store.record(conversation_id, MessageRole.CUSTOMER, content)

    cached = await store.working_state(conversation_id)
    await redis.flushdb()
    rebuilt = await store.working_state(conversation_id)

    assert [m["content"] for m in cached.history] == ["b", "c"]
    assert [m["content"] for m in rebuilt.history] == ["b", "c"]
