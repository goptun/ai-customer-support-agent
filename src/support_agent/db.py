from typing import Any

from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# How the LangGraph Postgres checkpointer expects its psycopg connections.
CHECKPOINTER_CONNECTION_KWARGS: dict[str, Any] = {
    "autocommit": True,
    "prepare_threshold": 0,
    "row_factory": dict_row,
}


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def migrate(database_url: str) -> None:
    """Upgrade the database to the latest migration (run from the repo or image root)."""
    config = Config("alembic.ini")
    # configparser interpolates %: a percent-encoded password would otherwise break it.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")
