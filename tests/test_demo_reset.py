"""The daily Demo Reset rebuilds the demo database from scratch and clears Redis (ADR-0006).

Each test runs against a database and Redis db of its own, so the reset never touches the
data the rest of the suite shares.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from support_agent.config import Settings
from support_agent.db import migrate, session_factory
from support_agent.knowledge import index_policies, load_policies
from support_agent.limits import LLMBudget
from support_agent.reset import reset_demo
from support_agent.seed import ATTENDANTS, CUSTOMERS, ORDERS, SHIPMENTS, seed

from .conftest import app_client
from .fakes import LexicalEmbedder, ScriptedLLM, order_status, refund
from .test_api import ANA, chat, token_for
from .test_demo_api import admitted, bearer, order_in, restart_signal
from .test_escalation_graph import wants_human

RESET_REDIS_DB = 1


@pytest.fixture
async def demo_settings(settings: Settings) -> AsyncIterator[Settings]:
    """Settings pointing at a fresh, migrated and seeded database and an empty Redis db."""
    name = f"demo_reset_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f"CREATE DATABASE {name}"))
    database_url = make_url(settings.database_url).set(database=name)
    redis_url = f"{settings.redis_url.rsplit('/', 1)[0]}/{RESET_REDIS_DB}"
    dedicated = settings.model_copy(
        update={
            "database_url": database_url.render_as_string(hide_password=False),
            "redis_url": redis_url,
        }
    )
    migrate(dedicated.database_url)
    engine = create_async_engine(dedicated.database_url)
    sessions = session_factory(engine)
    await seed(sessions)
    await index_policies(sessions, LexicalEmbedder(), load_policies())
    await engine.dispose()
    try:
        yield dedicated
    finally:
        redis = Redis.from_url(redis_url)
        await redis.flushdb()
        await redis.aclose()
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        await admin.dispose()


@pytest.fixture
async def demo_engine(demo_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(demo_settings.database_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


async def reset(settings: Settings) -> None:
    await reset_demo(settings, LexicalEmbedder())


async def ids(engine: AsyncEngine, query: str) -> set[str]:
    async with engine.connect() as conn:
        return {str(row[0]) for row in await conn.execute(text(query))}


async def count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


async def a_busy_demo_day(client: AsyncClient, llm: ScriptedLLM) -> dict[str, object]:
    """A Demo Visitor and a seeded Customer chat, leaving Conversations, Tickets, an Audit
    Log, checkpoints and Redis working state behind. Returns the visitor."""
    visitor = await admitted(client)
    order_id = order_in(visitor, "processing")
    llm.on(f"onde está o {order_id}?", order_status(order_id))
    await chat(client, bearer(visitor), f"onde está o {order_id}?")
    refundable = order_in(visitor, "delivered", refundable=True)
    llm.on(f"quero reembolso do {refundable}", refund(refundable))
    await chat(client, bearer(visitor), f"quero reembolso do {refundable}")
    llm.on("quero um humano", wants_human())
    await chat(client, bearer(visitor), "quero um humano")
    ana = await token_for(client, ANA)
    for _ in range(6):  # enough for psycopg to prepare the statements on its connections
        await chat(client, ana, "oi")
    return visitor


async def test_reset_leaves_only_the_seeded_data(
    demo_settings: Settings, demo_engine: AsyncEngine, llm: ScriptedLLM
) -> None:
    async with app_client(demo_settings, llm) as client:
        await a_busy_demo_day(client, llm)
    for table in ("conversations", "tickets", "refunds", "approval_requests", "checkpoints"):
        assert await count(demo_engine, table) > 0, table

    await reset(demo_settings)

    assert await ids(demo_engine, "SELECT id FROM customers") == {c["id"] for c in CUSTOMERS}
    assert await ids(demo_engine, "SELECT id FROM attendants") == {a["id"] for a in ATTENDANTS}
    assert await ids(demo_engine, "SELECT id FROM orders") == {o["id"] for o in ORDERS}
    assert await count(demo_engine, "shipments") == len(SHIPMENTS)
    for table in (
        "conversations",
        "messages",
        "tickets",
        "refunds",
        "cancellations",
        "approval_requests",
        "checkpoints",
        "checkpoint_writes",
    ):
        assert await count(demo_engine, table) == 0, table
    assert await count(demo_engine, "knowledge_chunks") > 0


async def test_audit_log_is_empty_after_the_reset_and_still_append_only(
    demo_settings: Settings, demo_engine: AsyncEngine, llm: ScriptedLLM
) -> None:
    async with app_client(demo_settings, llm) as client:
        await a_busy_demo_day(client, llm)
    assert await count(demo_engine, "audit_log") > 0

    await reset(demo_settings)

    assert await count(demo_engine, "audit_log") == 0
    for statement in (
        "UPDATE audit_log SET action = 'x'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with demo_engine.begin() as conn:
                await conn.execute(text(statement))


async def test_redis_working_state_is_cleared_but_the_llm_budget_is_kept(
    demo_settings: Settings, llm: ScriptedLLM
) -> None:
    async with app_client(demo_settings, llm) as client:
        visitor = await admitted(client)
        await client.post(
            "/chat",
            json={"message": "oi"},
            headers={**bearer(visitor), "Idempotency-Key": "k-1"},
        )
    redis = Redis.from_url(demo_settings.redis_url)
    budget = LLMBudget.key(datetime.now(UTC).date())
    await redis.set(budget, 42)
    keys = {key.decode() async for key in redis.scan_iter()}
    assert any(k.startswith("rate:conversation:") for k in keys)
    assert any(k.startswith("idempotency:") for k in keys)
    assert any(k.startswith("conversation:") for k in keys)

    await reset(demo_settings)

    assert [key.decode() async for key in redis.scan_iter()] == [budget]
    assert await redis.get(budget) == b"42"
    await redis.aclose()


async def test_running_app_keeps_serving_and_earlier_visitors_get_the_restart_signal(
    demo_settings: Settings, llm: ScriptedLLM
) -> None:
    async with app_client(demo_settings, llm) as client:
        earlier = await a_busy_demo_day(client, llm)

        await reset(demo_settings)

        assert restart_signal(await client.get("/orders", headers=bearer(earlier)))
        assert restart_signal(
            await client.post("/chat", json={"message": "oi"}, headers=bearer(earlier))
        )
        ana = await token_for(client, ANA)
        llm.on("onde está o ORD-1003?", order_status("ORD-1003"))
        turn = await chat(client, ana, "onde está o ORD-1003?")
        assert "ORD-1003" in str(turn["reply"])
        assert (await client.get("/orders", headers=ana)).status_code == 200
        newcomer = await admitted(client)
        order_id = order_in(newcomer, "shipped")
        llm.on(f"cadê o {order_id}?", order_status(order_id))
        tracked = await chat(client, bearer(newcomer), f"cadê o {order_id}?")
        assert "em trânsito" in str(tracked["reply"]).casefold()
