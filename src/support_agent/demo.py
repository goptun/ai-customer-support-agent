"""Demo Visitors: a fictitious Customer and paired Attendant of their own (ADR-0005).

Admission is gated by Cloudflare Turnstile; the verifier is injected so tests use a fake.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

import httpx
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.models import (
    DEMO_ORDER_NUMBERS,
    Attendant,
    Customer,
    Order,
    OrderStatus,
    Shipment,
)

logger = logging.getLogger(__name__)

# As in the mock carrier, which shares no code with the agent: tracking says delivered then.
DEMO_DELIVERY_DAYS = 4

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class TurnstileVerifier(Protocol):
    async def verify(self, token: str) -> bool:
        """Whether Cloudflare vouches for the widget token."""
        ...


class CloudflareTurnstile:
    """Server-side check of a Turnstile token against Cloudflare's siteverify."""

    def __init__(self, http: httpx.AsyncClient, secret: SecretStr) -> None:
        self._http = http
        self._secret = secret

    async def verify(self, token: str) -> bool:
        try:
            response = await self._http.post(
                SITEVERIFY_URL,
                data={"secret": self._secret.get_secret_value(), "response": token},
            )
            response.raise_for_status()
            return response.json().get("success") is True
        except (httpx.HTTPError, ValueError):
            # Fail closed: without Cloudflare's word nobody is admitted.
            logger.warning("Turnstile siteverify failed", exc_info=True)
            return False


@dataclass(frozen=True)
class _Template:
    status: OrderStatus
    item: tuple[str, str, int, int]  # sku, name, qty, unit_cents
    placed: int  # days from admission
    promised: int
    dispatched: int | None = None  # delivered DEMO_DELIVERY_DAYS after it


# One Order per scenario the demo walks through.
TEMPLATE = (
    # Not Dispatched: can still be Cancelled.
    _Template(OrderStatus.PROCESSING, ("CANECA", "Caneca Térmica", 2, 5990), -1, 6),
    # Dispatched, in transit.
    _Template(OrderStatus.SHIPPED, ("MOCHILA", "Mochila Urbana", 1, 18900), -5, 3, -3),
    # Delivered long ago: outside the Refund window.
    _Template(OrderStatus.DELIVERED, ("LUMINARIA", "Luminária LED", 1, 15900), -60, -50, -57),
    # Delivered days ago: eligible for a Refund that needs an Attendant's approval.
    _Template(OrderStatus.DELIVERED, ("FONE-BT", "Fone Bluetooth", 1, 24990), -10, -3, -8),
)


@dataclass(frozen=True)
class DemoVisitor:
    customer_id: str
    customer_name: str
    attendant_id: str
    order_ids: list[str]


class DemoVisitors:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def admit(self, today: date) -> DemoVisitor:
        """A new Customer with the templated Orders, dated from `today`, and its Attendant."""
        suffix = uuid.uuid4().hex[:16]
        customer = Customer(
            id=f"demo_{suffix}", name="Visitante da demo", email=f"demo_{suffix}@demo.invalid"
        )
        attendant = Attendant(id=f"demo_att_{suffix}", name="Atendente da demo")
        noon = datetime.combine(today, time(12), tzinfo=UTC)
        async with self._sessions.begin() as session:
            session.add_all([customer, attendant])
            order_ids = []
            for template in TEMPLATE:
                number: int = await session.scalar(DEMO_ORDER_NUMBERS.next_value())
                order = _order(template, f"ORD-{number}", customer.id, noon)
                session.add(order)
                order_ids.append(order.id)
                if template.dispatched is not None:
                    dispatched_at = noon + timedelta(days=template.dispatched)
                    session.add(_shipment(order, dispatched_at, number))
        return DemoVisitor(customer.id, customer.name, attendant.id, order_ids)


def _order(template: _Template, order_id: str, customer_id: str, noon: datetime) -> Order:
    sku, name, qty, unit_cents = template.item
    items: list[dict[str, Any]] = [{"sku": sku, "name": name, "qty": qty, "unit_cents": unit_cents}]
    return Order(
        id=order_id,
        customer_id=customer_id,
        status=template.status,
        items=items,
        total_paid_cents=qty * unit_cents,
        placed_at=noon + timedelta(days=template.placed),
        promised_delivery_date=(noon + timedelta(days=template.promised)).date(),
        delivered_at=(
            noon + timedelta(days=template.dispatched + DEMO_DELIVERY_DAYS)
            if template.status is OrderStatus.DELIVERED and template.dispatched is not None
            else None
        ),
    )


def _shipment(order: Order, dispatched_at: datetime, number: int) -> Shipment:
    kind = "DELIVERED" if order.status is OrderStatus.DELIVERED else "TRANSIT"
    return Shipment(
        order_id=order.id,
        # The mock carrier reads the scenario and dispatch date off the code.
        tracking_code=f"DEMO-{kind}-{dispatched_at:%Y%m%d}-{number}",
        carrier="Correios Mock",
        dispatched_at=dispatched_at,
    )
