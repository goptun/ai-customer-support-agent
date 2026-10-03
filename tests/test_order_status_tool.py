from collections.abc import Callable
from datetime import date

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.cancellations import Cancellations
from support_agent.db import session_factory
from support_agent.logistics import LogisticsClient, ShipmentStatus
from support_agent.orders import Orders
from support_agent.seed import ANCHOR
from support_agent.tickets import Tickets
from support_agent.tools import (
    ErrorCode,
    OrderStatusResult,
    ToolDeps,
    ToolError,
    get_order_status,
)

from .fakes import lexical_knowledge_base, refunds

ANA = "cus_ana"
TODAY = ANCHOR.date()

Handler = Callable[[httpx.Request], httpx.Response]


def tracking_json(code: str, status: str) -> dict[str, object]:
    return {
        "tracking_code": code,
        "status": status,
        "events": [
            {
                "at": "2026-08-28T09:00:00Z",
                "status": "posted",
                "description": "Objeto postado",
                "location": "São Paulo/SP",
            },
            {
                "at": "2026-08-30T15:00:00Z",
                "status": status,
                "description": "Objeto em trânsito",
                "location": "Curitiba/PR",
            },
        ],
    }


def deps(engine: AsyncEngine, handler: Handler) -> ToolDeps:
    http = httpx.AsyncClient(
        base_url="http://logistics.test", transport=httpx.MockTransport(handler)
    )
    sessions = session_factory(engine)
    return ToolDeps(
        orders=Orders(sessions),
        logistics=LogisticsClient(http, timeout_seconds=0.5),
        tickets=Tickets(sessions),
        cancellations=Cancellations(sessions),
        refunds=refunds(sessions),
        knowledge_base=lexical_knowledge_base(sessions),
        today=lambda: TODAY,
    )


def carrier_returns(status: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        code = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=tracking_json(code, status))

    return handler


def carrier_must_not_be_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected carrier call: {request.url}")


async def test_dispatched_order_merges_order_data_with_latest_carrier_event(
    engine: AsyncEngine,
) -> None:
    result = await get_order_status(
        deps(engine, carrier_returns("in_transit")), ANA, {"order_id": "ORD-1002"}
    )

    assert isinstance(result, OrderStatusResult)
    assert result.order_id == "ORD-1002"
    assert result.status == "shipped"
    assert result.promised_delivery_date == date(2026, 9, 3)
    assert result.days_late == 0
    assert result.shipment is not None
    assert result.shipment.tracking_code == "BR100000002"
    assert result.shipment.tracking is not None
    assert result.shipment.tracking.status is ShipmentStatus.IN_TRANSIT
    assert result.shipment.tracking.latest_event.location == "Curitiba/PR"
    assert result.shipment.tracking_error is None


async def test_delay_is_derived_from_the_promised_date(engine: AsyncEngine) -> None:
    result = await get_order_status(
        deps(engine, carrier_returns("delayed")), "cus_bruno", {"order_id": "ORD-2002"}
    )

    assert isinstance(result, OrderStatusResult)
    assert result.promised_delivery_date == date(2026, 8, 17)
    assert result.days_late == 15


async def test_order_id_is_normalized_from_a_bare_number(engine: AsyncEngine) -> None:
    result = await get_order_status(
        deps(engine, carrier_returns("in_transit")), ANA, {"order_id": "1002"}
    )

    assert isinstance(result, OrderStatusResult)
    assert result.order_id == "ORD-1002"


async def test_invalid_arguments_are_a_structured_error(engine: AsyncEngine) -> None:
    tool_deps = deps(engine, carrier_must_not_be_called)

    garbage = await get_order_status(tool_deps, ANA, {"order_id": "abc'; DROP TABLE"})
    missing = await get_order_status(tool_deps, ANA, {})

    assert isinstance(garbage, ToolError) and garbage.code is ErrorCode.INVALID_ARGUMENT
    assert isinstance(missing, ToolError) and missing.code is ErrorCode.INVALID_ARGUMENT


async def test_unknown_and_foreign_orders_are_errors_without_carrier_calls(
    engine: AsyncEngine,
) -> None:
    tool_deps = deps(engine, carrier_must_not_be_called)

    unknown = await get_order_status(tool_deps, ANA, {"order_id": "ORD-9999"})
    foreign = await get_order_status(tool_deps, ANA, {"order_id": "ORD-2002"})

    assert isinstance(unknown, ToolError) and unknown.code is ErrorCode.NOT_FOUND
    assert isinstance(foreign, ToolError) and foreign.code is ErrorCode.NOT_OWNED


async def test_customer_comes_from_auth_not_from_llm_arguments(engine: AsyncEngine) -> None:
    result = await get_order_status(
        deps(engine, carrier_must_not_be_called),
        ANA,
        {"order_id": "ORD-2002", "customer_id": "cus_bruno"},
    )

    assert isinstance(result, ToolError)
    assert result.code is ErrorCode.NOT_OWNED


async def test_not_dispatched_order_has_no_shipment_and_skips_the_carrier(
    engine: AsyncEngine,
) -> None:
    result = await get_order_status(
        deps(engine, carrier_must_not_be_called), ANA, {"order_id": "ORD-1003"}
    )

    assert isinstance(result, OrderStatusResult)
    assert result.status == "processing"
    assert result.shipment is None
    assert result.promised_delivery_date == date(2026, 9, 8)


async def test_carrier_failures_keep_order_data_and_flag_upstream_unavailable(
    engine: AsyncEngine,
) -> None:
    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("carrier too slow", request=request)

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    for handler in (server_error, timeout, malformed):
        result = await get_order_status(deps(engine, handler), ANA, {"order_id": "ORD-1002"})

        assert isinstance(result, OrderStatusResult), handler.__name__
        assert result.promised_delivery_date == date(2026, 9, 3)
        assert result.shipment is not None
        assert result.shipment.tracking is None
        assert result.shipment.tracking_error is ErrorCode.UPSTREAM_UNAVAILABLE


async def test_carrier_without_tracking_for_the_code_is_reported(engine: AsyncEngine) -> None:
    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown tracking code"})

    result = await get_order_status(deps(engine, not_found), ANA, {"order_id": "ORD-1002"})

    assert isinstance(result, OrderStatusResult)
    assert result.shipment is not None
    assert result.shipment.tracking is None
    assert result.shipment.tracking_error is ErrorCode.TRACKING_NOT_FOUND
