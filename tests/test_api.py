import uuid

import jwt
from httpx import AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.config import Settings

from .conftest import TEST_JWT_SECRET

ANA = "cus_ana"
BRUNO = "cus_bruno"


async def token_for(client: AsyncClient, customer_id: str) -> dict[str, str]:
    response = await client.post("/auth/dev-token", json={"customer_id": customer_id})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def chat(
    client: AsyncClient, headers: dict[str, str], message: str, conversation_id: str | None = None
) -> dict[str, object]:
    body: dict[str, object] = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = await client.post("/chat", json=body, headers=headers)
    assert response.status_code == 200, response.text
    data: dict[str, object] = response.json()
    return data


async def stored_messages(engine: AsyncEngine, conversation_id: str) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT role, content FROM messages WHERE conversation_id = :id ORDER BY id"),
            {"id": uuid.UUID(conversation_id)},
        )
        return [(row.role, row.content) for row in rows]


async def test_dev_token_is_issued_for_seeded_customer_with_role_claim(
    client: AsyncClient,
) -> None:
    response = await client.post("/auth/dev-token", json={"customer_id": ANA})

    assert response.status_code == 200
    claims = jwt.decode(response.json()["access_token"], TEST_JWT_SECRET, algorithms=["HS256"])
    assert claims["sub"] == ANA
    assert claims["role"] == "customer"


async def test_dev_token_is_refused_for_unknown_customer(client: AsyncClient) -> None:
    response = await client.post("/auth/dev-token", json={"customer_id": "cus_nobody"})

    assert response.status_code == 404


async def test_chat_rejects_unauthenticated_requests(client: AsyncClient) -> None:
    forged_token = jwt.encode(
        {"sub": ANA, "role": "customer"}, "a-different-secret-of-sufficient-length"
    )

    missing = await client.post("/chat", json={"message": "oi"})
    forged = await client.post(
        "/chat", json={"message": "oi"}, headers={"Authorization": f"Bearer {forged_token}"}
    )

    assert missing.status_code == 401
    assert forged.status_code == 401


async def test_chat_round_trip_persists_conversation_and_messages(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    headers = await token_for(client, ANA)

    first = await chat(client, headers, "Meu pedido ainda não chegou")
    conversation_id = str(first["conversation_id"])
    second = await chat(client, headers, "Pode verificar?", conversation_id)

    assert "Meu pedido ainda não chegou" in str(first["reply"])
    assert first["intent"] == "other"
    assert first["pending_actions"] == []
    assert second["conversation_id"] == conversation_id
    assert await stored_messages(engine, conversation_id) == [
        ("customer", "Meu pedido ainda não chegou"),
        ("agent", first["reply"]),
        ("customer", "Pode verificar?"),
        ("agent", second["reply"]),
    ]


async def test_customer_cannot_use_another_customers_conversation(client: AsyncClient) -> None:
    ana = await token_for(client, ANA)
    bruno = await token_for(client, BRUNO)
    conversation_id = (await chat(client, ana, "oi"))["conversation_id"]

    response = await client.post(
        "/chat", json={"conversation_id": conversation_id, "message": "oi"}, headers=bruno
    )
    unknown = await client.post(
        "/chat", json={"conversation_id": str(uuid.uuid4()), "message": "oi"}, headers=bruno
    )

    assert response.status_code == 404
    assert unknown.status_code == 404


async def test_losing_working_state_does_not_lose_history(
    client: AsyncClient, engine: AsyncEngine, settings: Settings
) -> None:
    headers = await token_for(client, ANA)
    conversation_id = str((await chat(client, headers, "primeira"))["conversation_id"])

    redis = Redis.from_url(settings.redis_url)
    keys = await redis.keys(f"*{conversation_id}*")
    assert keys, "working state should be cached in Redis"
    assert all([await redis.ttl(key) > 0 for key in keys]), "working state must expire"
    await redis.delete(*keys)

    await chat(client, headers, "segunda", conversation_id)
    await redis.aclose()

    contents = [content for _, content in await stored_messages(engine, conversation_id)]
    assert contents[0] == "primeira"
    assert contents[2] == "segunda"


async def test_graph_checkpoints_each_conversation_in_postgres(
    client: AsyncClient, settings: Settings
) -> None:
    headers = await token_for(client, ANA)
    first = str((await chat(client, headers, "conversa um"))["conversation_id"])
    second = str((await chat(client, headers, "conversa dois"))["conversation_id"])

    conn_string = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        one = await saver.aget_tuple({"configurable": {"thread_id": first}})
        two = await saver.aget_tuple({"configurable": {"thread_id": second}})

    assert one is not None and two is not None
    assert first != second
    assert one.checkpoint["channel_values"]["messages"][0].content == "conversa um"
    assert two.checkpoint["channel_values"]["messages"][0].content == "conversa dois"
