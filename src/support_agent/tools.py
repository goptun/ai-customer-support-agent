"""Agent tools.

Every tool validates its (LLM-produced) arguments with Pydantic and returns a typed result or
a ToolError. Tools touching Orders take the Customer from the authenticated run context and
check Order ownership. Each call is a tool span (args and result) when tracing is on.
"""

import asyncio
import enum
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, cast

from langsmith import traceable
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from support_agent.cancellations import (
    CancellationRefusal,
    CancellationResult,
    Cancellations,
)
from support_agent.knowledge import KnowledgeBase, PolicyExcerpt
from support_agent.logistics import (
    CarrierUnavailable,
    LogisticsClient,
    ShipmentStatus,
    Tracking,
    TrackingNotFound,
)
from support_agent.models import (
    ActorType,
    Order,
    OrderStatus,
    Shipment,
    TicketPriority,
    TicketReason,
)
from support_agent.orders import Orders
from support_agent.refunds import (
    RefundRecord,
    RefundRefusal,
    RefundRequest,
    RefundRequested,
    Refunds,
)
from support_agent.tickets import NewTicket, TicketResult, Tickets

_ORDER_ID = re.compile(r"^(?:ORD-?|#)?(\d{1,12})$", re.IGNORECASE)


class ErrorCode(enum.StrEnum):
    NOT_FOUND = "not_found"
    NOT_OWNED = "not_owned"
    INVALID_ARGUMENT = "invalid_argument"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    TRACKING_NOT_FOUND = "tracking_not_found"
    ALREADY_DISPATCHED = "already_dispatched"
    ALREADY_CANCELLED = "already_cancelled"
    REFUND_CHANGED = "refund_changed"


REFUSAL_CODES = {
    CancellationRefusal.ALREADY_DISPATCHED: ErrorCode.ALREADY_DISPATCHED,
    CancellationRefusal.ALREADY_CANCELLED: ErrorCode.ALREADY_CANCELLED,
    CancellationRefusal.REFUND_CHANGED: ErrorCode.REFUND_CHANGED,
}


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


class KnowledgeBaseResult(BaseModel):
    kind: Literal["knowledge_base"] = "knowledge_base"
    chunks: list[PolicyExcerpt]  # best first; empty when the Knowledge Base doesn't cover it


class RefundRefused(BaseModel):
    """The Refund Policy found the Order not eligible; nothing was created."""

    kind: Literal["refund_refused"] = "refund_refused"
    order_id: str
    reason: RefundRefusal
    # The thresholds the decision used, so the explanation states the same ones.
    delivery_window_days: int
    late_after_days: int


class RefundStatusResult(BaseModel):
    kind: Literal["refund_status"] = "refund_status"
    order_id: str | None  # None: all of the Customer's Refunds
    refunds: list[RefundRecord]  # newest first


class CancellationPreview(BaseModel):
    """What cancelling the Order now would do; shown to the Customer for confirmation."""

    kind: Literal["cancellation_preview"] = "cancellation_preview"
    order_id: str
    refund_cents: int  # 0: nothing to refund


@dataclass(frozen=True)
class ToolDeps:
    orders: Orders
    logistics: LogisticsClient
    tickets: Tickets
    knowledge_base: KnowledgeBase
    cancellations: Cancellations
    refunds: Refunds
    today: Callable[[], date]


def _normalize_order_id(value: str) -> str:
    match = _ORDER_ID.match(value.strip())
    if match is None:
        raise ValueError("not an order number")
    return f"ORD-{match.group(1)}"


class OrderIdArgs(BaseModel):
    # Unknown keys (e.g. a customer_id the LLM made up) are ignored, never trusted.
    model_config = ConfigDict(extra="ignore")

    order_id: str

    @field_validator("order_id")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _normalize_order_id(value)


class CancelOrderArgs(OrderIdArgs):
    # The Refund the Customer was shown and confirmed; anything else cancels nothing.
    expected_refund_cents: int = Field(ge=0)


REFUND_REASON_CHARS = 1000


class CreateRefundArgs(OrderIdArgs):
    # Only what the Customer asked for: the ceiling is computed from the Order's records.
    amount_cents: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=REFUND_REASON_CHARS)


class RefundStatusArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: str | None = None

    @field_validator("order_id")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_order_id(value)


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


class SearchKnowledgeBaseArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=4000)

    @field_validator("query")
    @classmethod
    def _strip(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty query")
        return value.strip()


def _tool[**P, R: BaseModel](call: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    # Only the args the agent passed: deps are plumbing, the Customer is in the turn metadata.
    def inputs(given: dict[str, Any]) -> dict[str, Any]:
        return {"args": dict(given["args"])}

    def outputs(result: Any) -> dict[str, Any]:
        return result.model_dump(mode="json") if isinstance(result, BaseModel) else result

    traced = traceable(run_type="tool", process_inputs=inputs, process_outputs=outputs)(call)
    return cast(Callable[P, Awaitable[R]], traced)  # same call signature, plus langsmith_extra


def days_late(promised: date | None, delivered_at: datetime | None, today: date) -> int | None:
    if promised is None:
        return None
    reference = delivered_at.date() if delivered_at is not None else today
    return max(0, (reference - promised).days)


@_tool
async def get_order_status(
    deps: ToolDeps, customer_id: str, args: Mapping[str, object]
) -> OrderStatusResult | ToolError:
    try:
        order_id = OrderIdArgs.model_validate(args).order_id
    except ValidationError:
        return ToolError(code=ErrorCode.INVALID_ARGUMENT)

    found = await _owned_order(deps, customer_id, order_id)
    if isinstance(found, ToolError):
        return found
    order, shipment = found

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


async def _owned_order(
    deps: ToolDeps, customer_id: str, order_id: str
) -> tuple[Order, Shipment | None] | ToolError:
    found = await deps.orders.find_with_shipment(order_id)
    if found is None:
        return ToolError(code=ErrorCode.NOT_FOUND, order_id=order_id)
    if found[0].customer_id != customer_id:
        return ToolError(code=ErrorCode.NOT_OWNED, order_id=order_id)
    return found


def _refused(refusal: CancellationRefusal, order_id: str) -> ToolError:
    return ToolError(code=REFUSAL_CODES[refusal], order_id=order_id)


@_tool
async def preview_cancellation(
    deps: ToolDeps, customer_id: str, args: Mapping[str, object]
) -> CancellationPreview | ToolError:
    """Check whether the Order can be Cancelled and what would be refunded; changes nothing."""
    try:
        order_id = OrderIdArgs.model_validate(args).order_id
    except ValidationError:
        return ToolError(code=ErrorCode.INVALID_ARGUMENT)
    found = await _owned_order(deps, customer_id, order_id)
    if isinstance(found, ToolError):
        return found
    plan = await deps.cancellations.preview(found[0])
    if isinstance(plan, CancellationRefusal):
        return _refused(plan, order_id)
    return CancellationPreview(order_id=order_id, refund_cents=plan.refund_cents)


@_tool
async def cancel_order(
    deps: ToolDeps, customer_id: str, args: Mapping[str, object]
) -> CancellationResult | ToolError:
    """Cancel the whole Order, issuing its full Refund if paid. Only after explicit confirmation."""
    try:
        parsed = CancelOrderArgs.model_validate(args)
    except ValidationError:
        return ToolError(code=ErrorCode.INVALID_ARGUMENT)
    found = await _owned_order(deps, customer_id, parsed.order_id)
    if isinstance(found, ToolError):
        return found
    result = await deps.cancellations.cancel(
        parsed.order_id, customer_id, parsed.expected_refund_cents
    )
    if isinstance(result, CancellationRefusal):
        return _refused(result, parsed.order_id)
    return result


@_tool
async def create_refund(
    deps: ToolDeps, customer_id: str, conversation_id: uuid.UUID, args: Mapping[str, object]
) -> RefundRequested | RefundRefused | ToolError:
    """Request a Refund under the Refund Policy. Never issues money: an Attendant approves it."""
    try:
        parsed = CreateRefundArgs.model_validate(args)
    except ValidationError:
        return ToolError(code=ErrorCode.INVALID_ARGUMENT)
    found = await _owned_order(deps, customer_id, parsed.order_id)
    if isinstance(found, ToolError):
        return found
    order, shipment = found

    shipment_lost: bool | None = False
    if order.status is OrderStatus.SHIPPED and shipment is not None:
        # Only the carrier knows whether a Shipment on its way was lost.
        try:
            tracking = await deps.logistics.track(shipment.tracking_code)
            shipment_lost = tracking.status is ShipmentStatus.LOST
        except TrackingNotFound:
            pass
        except CarrierUnavailable:
            shipment_lost = None

    result = await deps.refunds.request(
        RefundRequest(
            order_id=parsed.order_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            requested_cents=parsed.amount_cents,
            reason=parsed.reason,
            shipment_lost=shipment_lost,
        ),
        deps.today(),
    )
    if result is RefundRefusal.CARRIER_UNAVAILABLE:
        return ToolError(code=ErrorCode.UPSTREAM_UNAVAILABLE, order_id=parsed.order_id)
    if isinstance(result, RefundRefusal):
        return RefundRefused(
            order_id=parsed.order_id,
            reason=result,
            delivery_window_days=deps.refunds.policy.delivery_window_days,
            late_after_days=deps.refunds.policy.late_after_days,
        )
    return result


@_tool
async def get_refund_status(
    deps: ToolDeps, customer_id: str, args: Mapping[str, object]
) -> RefundStatusResult | ToolError:
    """The Customer's Refunds and their state, for one Order or all of them."""
    try:
        order_id = RefundStatusArgs.model_validate(args).order_id
    except ValidationError:
        return ToolError(code=ErrorCode.INVALID_ARGUMENT)
    if order_id is not None:
        found = await _owned_order(deps, customer_id, order_id)
        if isinstance(found, ToolError):
            return found
    return RefundStatusResult(
        order_id=order_id, refunds=await deps.refunds.of_customer(customer_id, order_id)
    )


@_tool
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


@_tool
async def search_knowledge_base(
    knowledge_base: KnowledgeBase, args: Mapping[str, object]
) -> KnowledgeBaseResult | ToolError:
    try:
        query = SearchKnowledgeBaseArgs.model_validate(args).query
    except ValidationError:
        return ToolError(code=ErrorCode.INVALID_ARGUMENT)
    return KnowledgeBaseResult(chunks=await knowledge_base.search(query))


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
