"""Agent tools.

Every tool validates its (LLM-produced) arguments with Pydantic, takes the Customer from the
authenticated run context, checks Order ownership and returns a typed result or a ToolError.
"""

import asyncio
import enum
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from support_agent.logistics import (
    CarrierUnavailable,
    LogisticsClient,
    Tracking,
    TrackingNotFound,
)
from support_agent.models import ActorType, OrderStatus, TicketPriority, TicketReason
from support_agent.orders import Orders
from support_agent.tickets import NewTicket, TicketResult, Tickets

_ORDER_ID = re.compile(r"^(?:ORD-?|#)?(\d{1,12})$", re.IGNORECASE)


class ErrorCode(enum.StrEnum):
    NOT_FOUND = "not_found"
    NOT_OWNED = "not_owned"
    INVALID_ARGUMENT = "invalid_argument"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    TRACKING_NOT_FOUND = "tracking_not_found"


class ToolError(BaseModel):
    kind: Literal["error"] = "error"
    code: ErrorCode
    order_id: str | None = None


TrackingErrorCode = Literal[ErrorCode.UPSTREAM_UNAVAILABLE, ErrorCode.TRACKING_NOT_FOUND]


class ShipmentInfo(BaseModel):
    tracking_code: str
    carrier: str
    tracking: Tracking | None
    tracking_error: TrackingErrorCode | None


class OrderStatusResult(BaseModel):
    kind: Literal["order_status"] = "order_status"
    order_id: str
    status: OrderStatus
    placed_at: datetime
    promised_delivery_date: date | None
    delivered_at: datetime | None
    days_late: int | None
    shipment: ShipmentInfo | None  # None: the Order has not been Dispatched


@dataclass(frozen=True)
class ToolDeps:
    orders: Orders
    logistics: LogisticsClient
    tickets: Tickets
    today: Callable[[], date]


def _normalize_order_id(value: str) -> str:
    match = _ORDER_ID.match(value.strip())
    if match is None:
        raise ValueError("not an order number")
    return f"ORD-{match.group(1)}"


class GetOrderStatusArgs(BaseModel):
    # Unknown keys (e.g. a customer_id the LLM made up) are ignored, never trusted.
    model_config = ConfigDict(extra="ignore")

    order_id: str

    @field_validator("order_id")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _normalize_order_id(value)


class CreateSupportTicketArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: TicketReason
    priority: TicketPriority
    summary: str = Field(min_length=1, max_length=4000)
    order_id: str | None = None

    @field_validator("order_id")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_order_id(value)


def days_late(promised: date | None, delivered_at: datetime | None, today: date) -> int | None:
    if promised is None:
        return None
    reference = delivered_at.date() if delivered_at is not None else today
    return max(0, (reference - promised).days)


async def get_order_status(
    deps: ToolDeps, customer_id: str, args: Mapping[str, object]
) -> OrderStatusResult | ToolError:
    try:
        order_id = GetOrderStatusArgs.model_validate(args).order_id
    except ValidationError:
        return ToolError(code=ErrorCode.INVALID_ARGUMENT)

    found = await deps.orders.find_with_shipment(order_id)
    if found is None:
        return ToolError(code=ErrorCode.NOT_FOUND, order_id=order_id)
    order, shipment = found
    if order.customer_id != customer_id:
        return ToolError(code=ErrorCode.NOT_OWNED, order_id=order_id)

    shipment_info = None
    if shipment is not None:
        tracking: Tracking | None = None
        tracking_error: TrackingErrorCode | None = None
        try:
            tracking = await deps.logistics.track(shipment.tracking_code)
        except TrackingNotFound:
            tracking_error = ErrorCode.TRACKING_NOT_FOUND
        except CarrierUnavailable:
            tracking_error = ErrorCode.UPSTREAM_UNAVAILABLE
        shipment_info = ShipmentInfo(
            tracking_code=shipment.tracking_code,
            carrier=shipment.carrier,
            tracking=tracking,
            tracking_error=tracking_error,
        )

    return OrderStatusResult(
        order_id=order.id,
        status=order.status,
        placed_at=order.placed_at,
        promised_delivery_date=order.promised_delivery_date,
        delivered_at=order.delivered_at,
        days_late=days_late(order.promised_delivery_date, order.delivered_at, deps.today()),
        shipment=shipment_info,
    )


async def create_support_ticket(
    deps: ToolDeps, customer_id: str, conversation_id: uuid.UUID, args: Mapping[str, object]
) -> TicketResult | ToolError:
    try:
        parsed = CreateSupportTicketArgs.model_validate(args)
    except ValidationError:
        return ToolError(code=ErrorCode.INVALID_ARGUMENT)

    if await deps.tickets.conversation_owner(conversation_id) != customer_id:
        return ToolError(code=ErrorCode.NOT_OWNED)
    if parsed.order_id is not None:
        found = await deps.orders.find_with_shipment(parsed.order_id)
        if found is None:
            return ToolError(code=ErrorCode.NOT_FOUND, order_id=parsed.order_id)
        if found[0].customer_id != customer_id:
            return ToolError(code=ErrorCode.NOT_OWNED, order_id=parsed.order_id)

    # Asking for a human is the Customer's act; escalating on failures is the system's.
    requested = parsed.reason is TicketReason.CUSTOMER_REQUESTED
    return await deps.tickets.open(
        NewTicket(
            conversation_id=conversation_id,
            customer_id=customer_id,
            order_id=parsed.order_id,
            reason=parsed.reason,
            priority=parsed.priority,
            summary=parsed.summary,
        ),
        actor_type=ActorType.CUSTOMER if requested else ActorType.SYSTEM,
        actor_id=customer_id if requested else None,
    )


@dataclass(frozen=True)
class RetryPolicy:
    retries: int = 2
    backoff_seconds: float = 0.5


def is_failure(result: BaseModel) -> bool:
    """A failed call is one an upstream system could not serve; Customer errors don't count."""
    if isinstance(result, ToolError):
        return result.code is ErrorCode.UPSTREAM_UNAVAILABLE
    if isinstance(result, OrderStatusResult) and result.shipment is not None:
        return result.shipment.tracking_error is ErrorCode.UPSTREAM_UNAVAILABLE
    return False


async def call_with_retry[R: BaseModel](call: Callable[[], Awaitable[R]], policy: RetryPolicy) -> R:
    """Run a tool call, retrying failed attempts with exponential backoff."""
    for attempt in range(policy.retries):
        result = await call()
        if not is_failure(result):
            return result
        await asyncio.sleep(policy.backoff_seconds * 2**attempt)
    return await call()
