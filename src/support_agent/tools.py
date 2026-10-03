"""Agent tools.

Every tool validates its (LLM-produced) arguments with Pydantic, takes the Customer from the
authenticated run context, checks Order ownership and returns a typed result or a ToolError.
"""

import enum
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from support_agent.logistics import (
    CarrierUnavailable,
    LogisticsClient,
    Tracking,
    TrackingNotFound,
)
from support_agent.models import OrderStatus
from support_agent.orders import Orders

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


class ShipmentInfo(BaseModel):
    tracking_code: str
    carrier: str
    tracking: Tracking | None
    tracking_error: Literal[ErrorCode.UPSTREAM_UNAVAILABLE, ErrorCode.TRACKING_NOT_FOUND] | None


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
    today: Callable[[], date]


class GetOrderStatusArgs(BaseModel):
    # Unknown keys (e.g. a customer_id the LLM made up) are ignored, never trusted.
    model_config = ConfigDict(extra="ignore")

    order_id: str

    @field_validator("order_id")
    @classmethod
    def _normalize(cls, value: str) -> str:
        match = _ORDER_ID.match(value.strip())
        if match is None:
            raise ValueError("not an order number")
        return f"ORD-{match.group(1)}"


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
        tracking_error: (
            Literal[ErrorCode.UPSTREAM_UNAVAILABLE, ErrorCode.TRACKING_NOT_FOUND] | None
        ) = None
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
