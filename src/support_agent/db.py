from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def migrate(database_url: str) -> None:
    """Upgrade the database to the latest migration (run from the repo or image root)."""
    config = Config("alembic.ini")
    # configparser interpolates %: a percent-encoded password would otherwise break it.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")
