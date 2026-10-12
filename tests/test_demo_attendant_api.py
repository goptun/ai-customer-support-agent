"""The demo's Attendant view: a Demo Visitor decides only their own Customer's Approval Requests
and sees only their own Customer's Tickets (ADR-0005)."""

from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from .fakes import ScriptedLLM, refund
from .test_api import ANA, chat, token_for
from .test_approvals_api import attendant_token
from .test_cancel_order_tool import audit_of
from .test_demo_api import admitted, bearer, claims_of, order_in
from .test_escalation_graph import wants_human
from .test_refund_tools import refunds_of


async def refund_asked(
    client: AsyncClient, llm: ScriptedLLM, visitor: dict[str, Any]
) -> tuple[str, str]:
    """The visitor asks a Refund of their recently delivered Order; its Order and Conversation."""
    order_id = order_in(visitor, "delivered", refundable=True)
    llm.on(f"quero reembolso do {order_id}", refund(order_id, amount_cents=5000))
    turn = await chat(client, bearer(visitor), f"quero reembolso do {order_id}")
    return order_id, str(turn["conversation_id"])


async def pending_of(client: AsyncClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = await client.get("/approvals", headers=headers)
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()
    return listed


async def test_visitor_lists_only_their_own_customers_approval_requests(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    visitor, other = await admitted(client), await admitted(client)
    order_id, _ = await refund_asked(client, llm, visitor)
    others_order, _ = await refund_asked(client, llm, other)

    [listed] = await pending_of(client, bearer(visitor))

    assert listed["order_id"] == order_id
    assert listed["customer_id"] == claims_of(visitor)["sub"]
    assert listed["amount_cents"] == 5000
    assert listed["policy_result"]["ground"] == "delivered_recently"
    [others] = await pending_of(client, bearer(other))
    assert others["order_id"] == others_order


async def test_visitor_cannot_decide_another_visitors_approval_request(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    visitor, intruder = await admitted(client), await admitted(client)
    order_id, _ = await refund_asked(client, llm, visitor)
    [listed] = await pending_of(client, bearer(visitor))

    response = await client.post(
        f"/approvals/{listed['approval_request_id']}",
        json={"decision": "approve"},
        headers=bearer(intruder),
    )

    assert response.status_code == 404
    [still] = await pending_of(client, bearer(visitor))
    assert still["status"] == "pending"
    [pending] = await refunds_of(engine, order_id)
    assert pending.status == "pending_approval"


async def test_visitor_approves_as_their_attendant_and_the_conversation_is_told(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    visitor = await admitted(client)
    attendant_id = claims_of(visitor)["attendant"]
    order_id, conversation_id = await refund_asked(client, llm, visitor)
    [listed] = await pending_of(client, bearer(visitor))

    response = await client.post(
        f"/approvals/{listed['approval_request_id']}",
        json={"decision": "approve"},
        headers=bearer(visitor),
    )

    assert response.status_code == 200, response.text
    decided = response.json()
    assert (decided["status"], decided["refund_status"]) == ("approved", "issued")
    assert decided["decided_by"] == attendant_id
    [decision] = [
        e for e in await audit_of(engine, order_id) if e.action == "decide_approval_request"
    ]
    assert (decision.actor_type, decision.actor_id) == ("attendant", attendant_id)
    relayed = await client.get(
        f"/conversations/{conversation_id}/messages", headers=bearer(visitor)
    )
    assert "aprovado" in relayed.json()[-1]["content"].casefold()
    assert await pending_of(client, bearer(visitor)) == []


async def test_visitor_cannot_reject_without_a_reason(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    visitor = await admitted(client)
    await refund_asked(client, llm, visitor)
    [listed] = await pending_of(client, bearer(visitor))

    response = await client.post(
        f"/approvals/{listed['approval_request_id']}",
        json={"decision": "reject", "reason": "  "},
        headers=bearer(visitor),
    )

    assert response.status_code == 422
    [still] = await pending_of(client, bearer(visitor))
    assert still["status"] == "pending"


async def test_visitor_sees_only_their_own_customers_tickets(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    visitor, other = await admitted(client), await admitted(client)
    llm.on("quero falar com um atendente", wants_human())
    turn = await chat(client, bearer(visitor), "quero falar com um atendente")

    mine = await client.get("/tickets", headers=bearer(visitor))
    others = await client.get("/tickets", headers=bearer(other))

    assert mine.status_code == 200, mine.text
    [ticket] = mine.json()
    assert ticket["conversation_id"] == turn["conversation_id"]
    assert ticket["customer_id"] == claims_of(visitor)["sub"]
    assert (ticket["reason"], ticket["priority"]) == ("customer_requested", "normal")
    assert others.json() == []


async def test_a_customer_who_is_not_a_visitor_has_no_attendant_view(
    client: AsyncClient,
) -> None:
    customer = await token_for(client, ANA)

    approvals = await client.get("/approvals", headers=customer)
    tickets = await client.get("/tickets", headers=customer)

    assert approvals.status_code == 403
    assert tickets.status_code == 403


async def test_a_real_attendant_still_sees_and_decides_the_global_queue(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    visitor = await admitted(client)
    order_id, _ = await refund_asked(client, llm, visitor)
    attendant = await attendant_token(client)

    [listed] = [a for a in await pending_of(client, attendant) if a["order_id"] == order_id]
    response = await client.post(
        f"/approvals/{listed['approval_request_id']}",
        json={"decision": "reject", "reason": "fora da política"},
        headers=attendant,
    )
    tickets = await client.get("/tickets", headers=attendant)

    assert response.status_code == 200, response.text
    assert response.json()["decided_by"] == "att_julia"
    assert tickets.status_code == 200
