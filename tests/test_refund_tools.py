"""Refund tools against real Postgres: the policy on real records, approval, idempotency, audit."""

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.approvals import ApprovalPause, ApprovalWorkflow, build_approval_graph
from support_agent.cancellations import Cancellations
from support_agent.db import session_factory
from support_agent.logistics import LogisticsClient
from support_agent.models import (
    ApprovalRequest,
    ApprovalStatus,
    Order,
    OrderStatus,
    Refund,
    RefundStatus,
    Shipment,
)
from support_agent.orders import Orders
from support_agent.refunds import RefundPolicy, RefundRefusal, RefundRequested, Refunds
from support_agent.seed import ANCHOR
from support_agent.tickets import Tickets
from support_agent.tools import (
    ErrorCode,
    RefundRefused,
    RefundStatusResult,
    ToolDeps,
    ToolError,
    create_refund,
    get_refund_status,
)

from .fakes import lexical_knowledge_base
from .test_cancel_order_tool import audit_of, new_order
from .test_order_status_tool import Handler, tracking_json
from .test_ticket_tool import new_conversation

ANA = "cus_ana"
BRUNO = "cus_bruno"


def carrier_says(status: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=tracking_json(request.url.path.rsplit("/", 1)[-1], status))

    return handler


def carrier_down(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


def no_carrier(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected carrier call: {request.url}")


class Tools:
    def __init__(self, engine: AsyncEngine, carrier: Handler = no_carrier) -> None:
        self.engine = engine
        sessions = session_factory(engine)
        self.workflow = ApprovalWorkflow(build_approval_graph(InMemorySaver()))
        self.deps = ToolDeps(
            orders=Orders(sessions),
            logistics=LogisticsClient(
                httpx.AsyncClient(
                    base_url="http://logistics.test", transport=httpx.MockTransport(carrier)
                ),
                timeout_seconds=0.5,
            ),
            tickets=Tickets(sessions),
            cancellations=Cancellations(sessions),
            refunds=Refunds(sessions, RefundPolicy(), self.workflow),
            knowledge_base=lexical_knowledge_base(sessions),
            today=ANCHOR.date,
        )

    async def request(
        self, order_id: str, customer_id: str = ANA, **args: Any
    ) -> RefundRequested | RefundRefused | ToolError:
        conversation_id = await new_conversation(self.engine, customer_id)
        return await create_refund(
            self.deps,
            customer_id,
            conversation_id,
            {"order_id": order_id, "reason": "quero meu dinheiro de volta", **args},
        )


async def dispatched_order(
    engine: AsyncEngine,
    *,
    delivered_days_ago: int | None = None,
    promised_days_ago: int = 2,
    total_paid_cents: int = 20000,
    customer_id: str = ANA,
) -> str:
    """A Shipped (or Delivered) Order of its own, with its Shipment."""
    status = OrderStatus.SHIPPED if delivered_days_ago is None else OrderStatus.DELIVERED
    order_id = await new_order(engine, status, total_paid_cents, customer_id)
    async with session_factory(engine).begin() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        order.promised_delivery_date = ANCHOR.date() - timedelta(days=promised_days_ago)
        if delivered_days_ago is not None:
            order.delivered_at = ANCHOR - timedelta(days=delivered_days_ago)
        session.add(
            Shipment(
                order_id=order_id,
                tracking_code=f"BR{order_id[-10:]}",
                carrier="Transportadora Teste",
                dispatched_at=ANCHOR - timedelta(days=40),
            )
        )
    return order_id


async def refunds_of(engine: AsyncEngine, order_id: str) -> list[Refund]:
    async with session_factory(engine)() as session:
        return list(
            (
                await session.scalars(
                    select(Refund).where(Refund.order_id == order_id).order_by(Refund.id)
                )
            ).all()
        )


async def approval_requests_of(engine: AsyncEngine, order_id: str) -> list[ApprovalRequest]:
    async with session_factory(engine)() as session:
        return list(
            (
                await session.scalars(
                    select(ApprovalRequest).where(ApprovalRequest.order_id == order_id)
                )
            ).all()
        )


async def test_eligible_refund_awaits_approval_on_its_own_paused_thread(
    engine: AsyncEngine,
) -> None:
    tools = Tools(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=5, total_paid_cents=24990)

    result = await tools.request(order_id)

    [refund] = await refunds_of(engine, order_id)
    [approval] = await approval_requests_of(engine, order_id)
    assert result == RefundRequested(
        order_id=order_id,
        refund_id=refund.id,
        approval_request_id=approval.id,
        amount_cents=24990,
        max_cents=24990,
        requested_cents=None,
        capped=False,
    )
    assert refund.status is RefundStatus.PENDING_APPROVAL
    assert refund.idempotency_key == f"refund:{approval.id}"
    assert refund.reason == "quero meu dinheiro de volta"
    assert approval.status is ApprovalStatus.PENDING
    assert approval.refund_id == refund.id
    assert approval.customer_id == ANA
    assert approval.policy_result == {
        "ground": "delivered_recently",
        "max_cents": 24990,
        "requested_cents": None,
        "amount_cents": 24990,
        "capped": False,
    }
    assert await tools.workflow.pending(approval.id) == ApprovalPause(
        approval_request_id=approval.id,
        refund_id=refund.id,
        order_id=order_id,
        amount_cents=24990,
    )
    [entry] = await audit_of(engine, order_id)
    assert (entry.actor_type, entry.actor_id, entry.action) == ("customer", ANA, "create_refund")
    assert entry.result["approval_request_id"] == str(approval.id)


@pytest.mark.parametrize(
    ("requested", "amount", "capped"),
    [pytest.param(5000, 5000, False, id="lower"), pytest.param(99999, 20000, True, id="higher")],
)
async def test_requested_amount_is_kept_when_lower_and_capped_when_higher(
    engine: AsyncEngine, requested: int, amount: int, capped: bool
) -> None:
    order_id = await dispatched_order(engine, delivered_days_ago=1)

    result = await Tools(engine).request(order_id, amount_cents=requested)

    assert isinstance(result, RefundRequested)
    assert (result.amount_cents, result.max_cents, result.capped) == (amount, 20000, capped)
    assert [r.amount_cents for r in await refunds_of(engine, order_id)] == [amount]


async def test_llm_cannot_set_the_ceiling(engine: AsyncEngine) -> None:
    order_id = await dispatched_order(engine, delivered_days_ago=1)

    result = await Tools(engine).request(
        order_id, amount_cents=50000, max_cents=50000, total_paid_cents=50000
    )

    assert isinstance(result, RefundRequested)
    assert (result.amount_cents, result.max_cents) == (20000, 20000)


async def test_prior_refunds_lower_the_maximum(engine: AsyncEngine) -> None:
    order_id = await dispatched_order(engine, delivered_days_ago=1)
    async with session_factory(engine).begin() as session:
        session.add_all(
            [
                Refund(
                    idempotency_key=f"test:{uuid.uuid4()}",
                    order_id=order_id,
                    amount_cents=cents,
                    status=status,
                    reason="anterior",
                )
                for cents, status in [(3000, RefundStatus.ISSUED), (9000, RefundStatus.REJECTED)]
            ]
        )

    result = await Tools(engine).request(order_id)

    assert isinstance(result, RefundRequested)
    assert result.max_cents == 17000


async def test_lost_shipment_is_eligible(engine: AsyncEngine) -> None:
    order_id = await dispatched_order(engine, promised_days_ago=1)

    result = await Tools(engine, carrier_says("lost")).request(order_id)

    assert isinstance(result, RefundRequested)
    [approval] = await approval_requests_of(engine, order_id)
    assert approval.policy_result["ground"] == "shipment_lost"


async def test_shipment_far_past_promised_date_is_eligible_without_the_carrier(
    engine: AsyncEngine,
) -> None:
    order_id = await dispatched_order(engine, promised_days_ago=11)

    result = await Tools(engine, carrier_down).request(order_id)

    assert isinstance(result, RefundRequested)


@pytest.mark.parametrize(
    ("delivered_days_ago", "promised_days_ago", "carrier", "reason"),
    [
        pytest.param(
            31,
            40,
            no_carrier,
            RefundRefusal.DELIVERY_WINDOW_EXPIRED,
            id="delivered_too_long_ago",
        ),
        pytest.param(
            None,
            10,
            carrier_says("in_transit"),
            RefundRefusal.NOT_LATE_ENOUGH,
            id="in_transit_not_late_enough",
        ),
    ],
)
async def test_ineligible_order_is_refused_and_nothing_is_created(
    engine: AsyncEngine,
    delivered_days_ago: int | None,
    promised_days_ago: int,
    carrier: Handler,
    reason: RefundRefusal,
) -> None:
    order_id = await dispatched_order(
        engine, delivered_days_ago=delivered_days_ago, promised_days_ago=promised_days_ago
    )

    result = await Tools(engine, carrier).request(order_id)

    assert result == RefundRefused(
        order_id=order_id, reason=reason, delivery_window_days=30, late_after_days=10
    )
    assert await refunds_of(engine, order_id) == []
    assert await audit_of(engine, order_id) == []


async def test_order_not_dispatched_is_refused(engine: AsyncEngine) -> None:
    order_id = await new_order(engine, OrderStatus.PAID)

    result = await Tools(engine).request(order_id)

    assert isinstance(result, RefundRefused)
    assert result.reason is RefundRefusal.NOT_DISPATCHED


async def test_carrier_down_when_only_it_could_tell_is_a_failure(engine: AsyncEngine) -> None:
    order_id = await dispatched_order(engine, promised_days_ago=1)

    result = await Tools(engine, carrier_down).request(order_id)

    assert result == ToolError(code=ErrorCode.UPSTREAM_UNAVAILABLE, order_id=order_id)
    assert await refunds_of(engine, order_id) == []


async def test_repeated_request_returns_the_refund_already_awaiting_approval(
    engine: AsyncEngine,
) -> None:
    tools = Tools(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=1)
    first = await tools.request(order_id, amount_cents=5000)

    again = await tools.request(order_id, amount_cents=7000)

    assert isinstance(first, RefundRequested)
    assert again == first.model_copy(update={"already_pending": True})
    assert len(await refunds_of(engine, order_id)) == 1
    assert len(await audit_of(engine, order_id)) == 1


async def test_someone_elses_order_is_not_found(engine: AsyncEngine) -> None:
    order_id = await dispatched_order(engine, delivered_days_ago=1, customer_id=BRUNO)

    result = await Tools(engine).request(order_id)

    assert result == ToolError(code=ErrorCode.NOT_OWNED, order_id=order_id)
    assert await refunds_of(engine, order_id) == []


@pytest.mark.parametrize(
    "args",
    [{"amount_cents": 0}, {"amount_cents": -100}, {"reason": ""}],
)
async def test_invalid_arguments_are_rejected(engine: AsyncEngine, args: dict[str, Any]) -> None:
    order_id = await dispatched_order(engine, delivered_days_ago=1)

    result = await Tools(engine).request(order_id, **args)

    assert result == ToolError(code=ErrorCode.INVALID_ARGUMENT)


async def test_refund_status_reads_the_customers_refund_records(engine: AsyncEngine) -> None:
    tools = Tools(engine)
    pending = await dispatched_order(engine, delivered_days_ago=1)
    await tools.request(pending, amount_cents=5000)
    others = await dispatched_order(engine, delivered_days_ago=1, customer_id=BRUNO)
    await tools.request(others, BRUNO)

    everything = await get_refund_status(tools.deps, ANA, {})
    one = await get_refund_status(tools.deps, ANA, {"order_id": pending})
    not_mine = await get_refund_status(tools.deps, ANA, {"order_id": others})

    assert isinstance(everything, RefundStatusResult)
    assert pending in {r.order_id for r in everything.refunds}
    assert others not in {r.order_id for r in everything.refunds}
    assert isinstance(one, RefundStatusResult)
    [refund] = one.refunds
    assert (refund.order_id, refund.amount_cents, refund.status) == (
        pending,
        5000,
        RefundStatus.PENDING_APPROVAL,
    )
    assert not_mine == ToolError(code=ErrorCode.NOT_OWNED, order_id=others)
