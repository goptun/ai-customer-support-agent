"""Idempotent seed of fictional Customers, Orders and Shipments, and the Knowledge Base index.

Run with `python -m support_agent.seed`. Re-running never duplicates or overwrites rows, and
re-embeds only policy documents that changed.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from support_agent.config import get_settings
from support_agent.db import session_factory
from support_agent.knowledge import FastEmbedEmbedder, index_policies, load_policies
from support_agent.models import Customer, Order, OrderStatus, Shipment

# Dates are relative to a fixed anchor so the seed is deterministic across runs.
ANCHOR = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _at(days: int) -> datetime:
    return ANCHOR + timedelta(days=days)


def _on(days: int) -> date:
    return _at(days).date()


CUSTOMERS: list[dict[str, Any]] = [
    {"id": "cus_ana", "name": "Ana Souza", "email": "ana.souza@example.com"},
    {"id": "cus_bruno", "name": "Bruno Lima", "email": "bruno.lima@example.com"},
    {"id": "cus_carla", "name": "Carla Mendes", "email": "carla.mendes@example.com"},
    {"id": "cus_diego", "name": "Diego Alves", "email": "diego.alves@example.com"},
]

ORDERS: list[dict[str, Any]] = [
    {
        "id": "ORD-1001",
        "customer_id": "cus_ana",
        "status": OrderStatus.DELIVERED,
        "items": [{"sku": "FONE-BT", "name": "Fone Bluetooth", "qty": 1, "unit_cents": 24990}],
        "total_paid_cents": 24990,
        "placed_at": _at(-20),
        "promised_delivery_date": _on(-12),
        "delivered_at": _at(-13),
    },
    {
        "id": "ORD-1002",
        "customer_id": "cus_ana",
        "status": OrderStatus.SHIPPED,
        "items": [{"sku": "MOCHILA", "name": "Mochila Urbana", "qty": 1, "unit_cents": 18900}],
        "total_paid_cents": 18900,
        "placed_at": _at(-6),
        "promised_delivery_date": _on(2),
        "delivered_at": None,
    },
    {
        "id": "ORD-1003",
        "customer_id": "cus_ana",
        "status": OrderStatus.PROCESSING,
        "items": [{"sku": "CANECA", "name": "Caneca Térmica", "qty": 2, "unit_cents": 5990}],
        "total_paid_cents": 11980,
        "placed_at": _at(-1),
        "promised_delivery_date": _on(7),
        "delivered_at": None,
    },
    {
        "id": "ORD-2001",
        "customer_id": "cus_bruno",
        "status": OrderStatus.PAID,
        "items": [{"sku": "TECLADO", "name": "Teclado Mecânico", "qty": 1, "unit_cents": 39900}],
        "total_paid_cents": 39900,
        "placed_at": _at(-2),
        "promised_delivery_date": _on(6),
        "delivered_at": None,
    },
    {
        "id": "ORD-2002",
        "customer_id": "cus_bruno",
        "status": OrderStatus.SHIPPED,
        "items": [{"sku": "MONITOR", "name": "Monitor 27''", "qty": 1, "unit_cents": 149900}],
        "total_paid_cents": 149900,
        "placed_at": _at(-30),
        "promised_delivery_date": _on(-15),
        "delivered_at": None,
    },
    {
        "id": "ORD-2003",
        "customer_id": "cus_bruno",
        "status": OrderStatus.CANCELLED,
        "items": [{"sku": "MOUSE", "name": "Mouse sem fio", "qty": 1, "unit_cents": 8990}],
        "total_paid_cents": 8990,
        "placed_at": _at(-10),
        "promised_delivery_date": None,
        "delivered_at": None,
    },
    {
        "id": "ORD-3001",
        "customer_id": "cus_carla",
        "status": OrderStatus.PENDING_PAYMENT,
        "items": [{"sku": "LIVRO-PY", "name": "Livro de Python", "qty": 1, "unit_cents": 12900}],
        "total_paid_cents": 0,
        "placed_at": _at(0),
        "promised_delivery_date": None,
        "delivered_at": None,
    },
    {
        "id": "ORD-3002",
        "customer_id": "cus_carla",
        "status": OrderStatus.DELIVERED,
        "items": [{"sku": "LUMINARIA", "name": "Luminária LED", "qty": 1, "unit_cents": 15900}],
        "total_paid_cents": 15900,
        "placed_at": _at(-60),
        "promised_delivery_date": _on(-50),
        "delivered_at": _at(-51),
    },
    {
        "id": "ORD-1004",
        "customer_id": "cus_ana",
        "status": OrderStatus.SHIPPED,
        "items": [{"sku": "CAFETEIRA", "name": "Cafeteira", "qty": 1, "unit_cents": 32900}],
        "total_paid_cents": 32900,
        "placed_at": _at(-12),
        "promised_delivery_date": _on(-2),
        "delivered_at": None,
    },
    {
        "id": "ORD-2004",
        "customer_id": "cus_bruno",
        "status": OrderStatus.SHIPPED,
        "items": [{"sku": "CADEIRA", "name": "Cadeira Gamer", "qty": 1, "unit_cents": 89900}],
        "total_paid_cents": 89900,
        "placed_at": _at(-15),
        "promised_delivery_date": _on(-5),
        "delivered_at": None,
    },
    {
        "id": "ORD-3003",
        "customer_id": "cus_carla",
        "status": OrderStatus.SHIPPED,
        "items": [{"sku": "TAPETE", "name": "Tapete de Yoga", "qty": 1, "unit_cents": 9900}],
        "total_paid_cents": 9900,
        "placed_at": _at(-5),
        "promised_delivery_date": _on(3),
        "delivered_at": None,
    },
    {
        "id": "ORD-3004",
        "customer_id": "cus_carla",
        "status": OrderStatus.SHIPPED,
        "items": [{"sku": "GARRAFA", "name": "Garrafa Térmica", "qty": 1, "unit_cents": 7990}],
        "total_paid_cents": 7990,
        "placed_at": _at(-5),
        "promised_delivery_date": _on(3),
        "delivered_at": None,
    },
    {
        "id": "ORD-3005",
        "customer_id": "cus_carla",
        "status": OrderStatus.SHIPPED,
        "items": [{"sku": "ABAJUR", "name": "Abajur de Mesa", "qty": 1, "unit_cents": 13900}],
        "total_paid_cents": 13900,
        "placed_at": _at(-4),
        "promised_delivery_date": _on(4),
        "delivered_at": None,
    },
    # Diego's only Order: the single-recent-Order case.
    {
        "id": "ORD-4001",
        "customer_id": "cus_diego",
        "status": OrderStatus.PROCESSING,
        "items": [{"sku": "LUMINARIA", "name": "Luminária LED", "qty": 1, "unit_cents": 8990}],
        "total_paid_cents": 8990,
        "placed_at": _at(-2),
        "promised_delivery_date": _on(5),
        "delivered_at": None,
    },
]

# One Shipment per Dispatched Order (shipped or delivered). Tracking codes select the
# logistics mock scenario (see support_agent.logistics_mock).
SHIPMENTS: list[dict[str, Any]] = [
    {"order_id": "ORD-1001", "tracking_code": "BR100000001", "dispatched_at": _at(-17)},
    {"order_id": "ORD-1002", "tracking_code": "BR100000002", "dispatched_at": _at(-4)},
    {"order_id": "ORD-2002", "tracking_code": "BR200000002", "dispatched_at": _at(-27)},
    {"order_id": "ORD-3002", "tracking_code": "BR300000002", "dispatched_at": _at(-57)},
    {"order_id": "ORD-1004", "tracking_code": "BR100000004", "dispatched_at": _at(-10)},
    {"order_id": "ORD-2004", "tracking_code": "BR200000004", "dispatched_at": _at(-13)},
    {"order_id": "ORD-3003", "tracking_code": "BR300000003", "dispatched_at": _at(-3)},
    {"order_id": "ORD-3004", "tracking_code": "BR300000004", "dispatched_at": _at(-3)},
    {"order_id": "ORD-3005", "tracking_code": "BR300000005", "dispatched_at": _at(-2)},
]


async def seed(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        await session.execute(insert(Customer).values(CUSTOMERS).on_conflict_do_nothing())
        await session.execute(insert(Order).values(ORDERS).on_conflict_do_nothing())
        await session.execute(
            insert(Shipment)
            .values([{"carrier": "Correios Mock", **s} for s in SHIPMENTS])
            .on_conflict_do_nothing()
        )


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        sessions = session_factory(engine)
        await seed(sessions)
        embedder = await asyncio.to_thread(FastEmbedEmbedder, settings.embedding_model)
        await index_policies(sessions, embedder, load_policies())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
