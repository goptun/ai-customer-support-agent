"""Thin read repository for Orders and their Shipment."""

from datetime import date, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.models import Order, OrderStatus, Shipment


class OrderSummary(BaseModel):
    order_id: str
    placed_at: datetime
    status: OrderStatus


class OrderItemView(BaseModel):
    name: str
    qty: int


class OrderView(BaseModel):
    """An Order as the Customer sees it on the demo page."""

    order_id: str
    status: OrderStatus
    items: list[OrderItemView]
    total_paid_cents: int
    placed_at: datetime
    promised_delivery_date: date | None
    delivered_at: datetime | None


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

    async def recent(self, customer_id: str, since: datetime, limit: int) -> list[OrderSummary]:
        """The Customer's Orders placed since `since`, newest first."""
        async with self._sessions() as session:
            orders = (
                await session.scalars(
                    select(Order)
                    .where(Order.customer_id == customer_id, Order.placed_at >= since)
                    .order_by(Order.placed_at.desc())
                    .limit(limit)
                )
            ).all()
        return [
            OrderSummary(order_id=order.id, placed_at=order.placed_at, status=order.status)
            for order in orders
        ]

    async def of_customer(self, customer_id: str) -> list[OrderView]:
        """All of the Customer's Orders, oldest first."""
        async with self._sessions() as session:
            orders = (
                await session.scalars(
                    select(Order)
                    .where(Order.customer_id == customer_id)
                    .order_by(Order.placed_at, Order.id)
                )
            ).all()
        return [
            OrderView(
                order_id=order.id,
                status=order.status,
                items=[OrderItemView.model_validate(item) for item in order.items],
                total_paid_cents=order.total_paid_cents,
                placed_at=order.placed_at,
                promised_delivery_date=order.promised_delivery_date,
                delivered_at=order.delivered_at,
            )
            for order in orders
        ]
