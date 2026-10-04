import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from support_agent.api import create_app
from support_agent.config import Settings
from support_agent.db import session_factory
from support_agent.seed import seed

TEST_JWT_SECRET = "test-secret-with-enough-length-for-hs256"


@pytest.fixture(scope="session")
def infra_urls() -> Iterator[tuple[str, str]]:
    """Postgres and Redis URLs: from env (CI services) or throwaway testcontainers."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    redis_url = os.environ.get("TEST_REDIS_URL")
    if database_url and redis_url:
        yield database_url, redis_url
        return

    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.redis import RedisContainer

    with (
        PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as pg,
        RedisContainer("redis:7-alpine") as rd,
    ):
        redis_url = f"redis://{rd.get_container_host_ip()}:{rd.get_exposed_port(6379)}/0"
        yield pg.get_connection_url(), redis_url


@pytest.fixture(scope="session")
def settings(infra_urls: tuple[str, str]) -> Settings:
    database_url, redis_url = infra_urls
    return Settings(database_url=database_url, redis_url=redis_url, jwt_secret=TEST_JWT_SECRET)


@pytest.fixture(scope="session")
def migrated(settings: Settings) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
async def engine(settings: Settings, migrated: None) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.database_url)
    await seed(session_factory(engine))
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with (
        LifespanManager(app) as manager,
        AsyncClient(transport=ASGITransport(app=manager.app), base_url="http://test") as client,
    ):
        yield client
