"""Thin read repository for Orders and their Shipment."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.models import Order, Shipment


class Orders:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def find_with_shipment(self, order_id: str) -> tuple[Order, Shipment | None] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Order, Shipment)
                    .outerjoin(Shipment, Shipment.order_id == Order.id)
                    .where(Order.id == order_id)
                )
            ).first()
        if row is None:
            return None
        order, shipment = row
        return order, shipment
