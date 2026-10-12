"""The daily Demo Reset: rebuilds the demo database from scratch and clears Redis (ADR-0006).

Run with `python -m support_agent.reset` against the running stack (docs/deploy.md → Demo
Reset). The schema is dropped and rebuilt (migrations, then seed) rather than its rows deleted,
so the Audit Log's append-only trigger is never bypassed; the LangGraph checkpoints live in the
same schema and go with it. Demo Visitors caught mid-visit get the restart signal, as their
Customer is gone.
"""

import asyncio
import logging

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from support_agent.config import Settings, get_settings
from support_agent.db import CHECKPOINTER_CONNECTION_KWARGS, migrate, session_factory
from support_agent.knowledge import Embedder, FastEmbedEmbedder, index_policies, load_policies
from support_agent.limits import LLMBudget
from support_agent.seed import seed

logger = logging.getLogger(__name__)


async def reset_demo(settings: Settings, embedder: Embedder) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await asyncio.to_thread(migrate, settings.database_url)
        sessions = session_factory(engine)
        await seed(sessions)
        await index_policies(sessions, embedder, load_policies())
        # The running app only creates the checkpoint tables at start-up.
        async with await AsyncConnection.connect(
            settings.checkpointer_url, **CHECKPOINTER_CONNECTION_KWARGS
        ) as checkpoints:
            await AsyncPostgresSaver(checkpoints).setup()
        # The app's connections may hold plans for the dropped tables and types: end them,
        # its pools check connections out healthy and reconnect.
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            )
    finally:
        await engine.dispose()
    await _clear_working_state(settings.redis_url)


async def _clear_working_state(redis_url: str) -> None:
    """Every key but the LLM budget's: a reset mustn't hand out a second day of LLM calls."""
    redis = Redis.from_url(redis_url)
    try:
        async for key in redis.scan_iter(count=500):
            if not LLMBudget.owns(key.decode()):
                await redis.delete(key)
    finally:
        await redis.aclose()


async def main() -> None:
    settings = get_settings()
    embedder = await asyncio.to_thread(FastEmbedEmbedder, settings.embedding_model)
    await reset_demo(settings, embedder)
    logger.info("Demo Reset done")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
