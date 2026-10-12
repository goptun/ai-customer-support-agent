"""Abuse protection on POST /chat: per-Conversation rate limit, daily LLM call cap, idempotency."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.config import Settings
from support_agent.limits import Idempotency, IdempotencyKeyInFlight, LLMBudget

from .conftest import app_client
from .fakes import ScriptedLLM
from .test_api import ANA, BRUNO, chat, stored_messages, token_for
from .test_demo_api import admitted, bearer


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    redis = Redis.from_url(settings.redis_url)
    yield redis
    await redis.aclose()


async def test_a_conversation_over_its_rate_limit_gets_a_friendly_429(
    settings: Settings, engine: AsyncEngine, llm: ScriptedLLM
) -> None:
    limited = settings.model_copy(update={"conversation_turns_per_window": 2})
    async with app_client(limited, llm) as client:
        headers = await token_for(client, ANA)
        conversation_id = str((await chat(client, headers, "um"))["conversation_id"])
        await chat(client, headers, "dois", conversation_id)

        over = await client.post(
            "/chat", json={"conversation_id": conversation_id, "message": "três"}, headers=headers
        )
        other_conversation = await chat(client, headers, "outra conversa")

    assert over.status_code == 429
    assert over.json()["code"] == "conversation_rate_limited"
    assert "Você enviou muitas mensagens" in over.json()["detail"]
    assert 0 < int(over.headers["Retry-After"]) <= limited.conversation_rate_window_seconds
    assert other_conversation["reply"]
    contents = [content for _, content in await stored_messages(engine, conversation_id)]
    assert "três" not in contents


async def test_over_the_daily_llm_cap_no_turn_runs_until_the_next_day(
    settings: Settings, engine: AsyncEngine, llm: ScriptedLLM, redis: Redis
) -> None:
    key = LLMBudget.key(datetime.now(UTC).date())
    await redis.delete(key)
    capped = settings.model_copy(update={"llm_calls_per_day": 1})
    async with app_client(capped, llm) as client:
        headers = await token_for(client, ANA)
        await chat(client, headers, "primeira do dia")

        over = await client.post("/chat", json={"message": "mais uma"}, headers=headers)

    assert over.status_code == 429
    assert over.json()["code"] == "llm_cap_exhausted"
    assert "limite diário" in over.json()["detail"]
    assert 0 < int(over.headers["Retry-After"]) <= 24 * 3600
    assert int(await redis.get(key) or 0) >= 1
    assert await redis.ttl(key) > 0


async def test_admission_says_up_front_whether_the_daily_llm_cap_is_exhausted(
    settings: Settings, engine: AsyncEngine, llm: ScriptedLLM, redis: Redis
) -> None:
    key = LLMBudget.key(datetime.now(UTC).date())
    await redis.delete(key)
    capped = settings.model_copy(update={"llm_calls_per_day": 1})
    async with app_client(capped, llm) as client:
        before = await admitted(client)
        await chat(client, bearer(before), "primeira do dia")
        after = await admitted(client)

    assert before["llm_cap_exhausted"] is False
    assert after["llm_cap_exhausted"] is True


async def test_a_retry_with_the_same_idempotency_key_returns_the_original_turn(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    headers = {**await token_for(client, ANA), "Idempotency-Key": str(uuid.uuid4())}

    first = await client.post("/chat", json={"message": "Meu pedido sumiu"}, headers=headers)
    retry = await client.post("/chat", json={"message": "Meu pedido sumiu"}, headers=headers)

    assert first.status_code == retry.status_code == 200
    assert retry.json() == first.json()
    assert await stored_messages(engine, first.json()["conversation_id"]) == [
        ("customer", "Meu pedido sumiu"),
        ("agent", first.json()["reply"]),
    ]


async def test_an_idempotency_key_reused_for_another_message_is_refused(
    client: AsyncClient,
) -> None:
    headers = {**await token_for(client, ANA), "Idempotency-Key": str(uuid.uuid4())}
    await client.post("/chat", json={"message": "uma coisa"}, headers=headers)

    other = await client.post("/chat", json={"message": "outra coisa"}, headers=headers)

    assert other.status_code == 422


async def test_idempotency_keys_are_scoped_to_the_customer(client: AsyncClient) -> None:
    key = str(uuid.uuid4())
    ana = {**await token_for(client, ANA), "Idempotency-Key": key}
    bruno = {**await token_for(client, BRUNO), "Idempotency-Key": key}

    first = await client.post("/chat", json={"message": "oi"}, headers=ana)
    other = await client.post("/chat", json={"message": "oi"}, headers=bruno)

    assert other.status_code == 200
    assert other.json()["conversation_id"] != first.json()["conversation_id"]


async def test_a_replayed_turn_does_not_count_against_the_rate_limit(
    settings: Settings, engine: AsyncEngine, llm: ScriptedLLM
) -> None:
    limited = settings.model_copy(update={"conversation_turns_per_window": 1})
    async with app_client(limited, llm) as client:
        headers = {**await token_for(client, ANA), "Idempotency-Key": str(uuid.uuid4())}
        first = await client.post("/chat", json={"message": "oi"}, headers=headers)
        retries = [
            await client.post("/chat", json={"message": "oi"}, headers=headers) for _ in range(3)
        ]

    assert first.status_code == 200
    assert [r.status_code for r in retries] == [200, 200, 200]


async def test_a_refused_request_frees_its_idempotency_key_for_the_retry(
    settings: Settings, engine: AsyncEngine, llm: ScriptedLLM
) -> None:
    limited = settings.model_copy(update={"conversation_turns_per_window": 1})
    async with app_client(limited, llm) as client:
        headers = await token_for(client, ANA)
        conversation_id = str((await chat(client, headers, "um"))["conversation_id"])
        keyed = {**headers, "Idempotency-Key": str(uuid.uuid4())}
        body = {"conversation_id": conversation_id, "message": "dois"}

        refused = await client.post("/chat", json=body, headers=keyed)
        retry = await client.post("/chat", json=body, headers=keyed)

    assert refused.status_code == 429
    assert retry.status_code == 429  # ran again, not 409 for a claim left behind


async def test_a_key_whose_first_request_is_still_running_is_in_flight(redis: Redis) -> None:
    idempotency = Idempotency(redis, ttl_seconds=60, in_flight_ttl_seconds=5)
    key, request = str(uuid.uuid4()), {"message": "oi"}

    assert await idempotency.claim(ANA, key, request) is None
    with pytest.raises(IdempotencyKeyInFlight):
        await idempotency.claim(ANA, key, request)
    await idempotency.settle(ANA, key, request, {"reply": "olá"})

    assert await idempotency.claim(ANA, key, request) == {"reply": "olá"}
