"""Putting the evaluation database back as seeded, between scripted Conversations."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.seed import seed


async def reset(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Back to the seeded Orders, with no Conversations or side effects (Tickets, Refunds,
    Cancellations, Approval Requests) left. The Audit Log is append-only and stays.

    Only for the evaluation's disposable database: it wipes every Conversation.
    """
    async with sessions.begin() as session:
        # Every table holding a side effect references an Order or a Conversation.
        await session.execute(text("TRUNCATE conversations, orders CASCADE"))
    await seed(sessions)
