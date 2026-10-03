"""Attendant decisions end to end: list, approve or reject, resume the paused workflow, relay."""

import asyncio
import uuid
from dataclasses import dataclass

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.config import Settings
from support_agent.db import session_factory
from support_agent.models import Customer

from .conftest import app_client
from .fakes import ScriptedLLM, faq, refund
from .test_api import chat, stored_messages, token_for
from .test_cancel_order_tool import audit_of
from .test_refund_tools import approval_requests_of, dispatched_order, refunds_of

ANA = "cus_ana"
BRUNO = "cus_bruno"
JULIA = "att_julia"


async def attendant_token(client: AsyncClient, attendant_id: str = JULIA) -> dict[str, str]:
    response = await client.post("/auth/dev-token", json={"attendant_id": attendant_id})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@dataclass(frozen=True)
class Requested:
    customer_id: str
    order_id: str
    conversation_id: str
    approval_id: str


async def new_customer(engine: AsyncEngine) -> str:
    """A Customer of its own, so no other test's unseen decisions reach it."""
    customer_id = f"cus_{uuid.uuid4().hex[:12]}"
    async with session_factory(engine).begin() as session:
        session.add(Customer(id=customer_id, name="Teste", email=f"{customer_id}@example.com"))
    return customer_id


async def refund_requested(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine, amount_cents: int = 10000
) -> Requested:
    """A new Customer's delivered Order with a Refund awaiting approval."""
    customer_id = await new_customer(engine)
    order_id = await dispatched_order(
        engine, delivered_days_ago=4, total_paid_cents=24990, customer_id=customer_id
    )
    llm.on(f"quero reembolso do {order_id}", refund(order_id, amount_cents=amount_cents))
    asked = await chat(
        client, await token_for(client, customer_id), f"quero reembolso do {order_id}"
    )
    [approval] = await approval_requests_of(engine, order_id)
    return Requested(customer_id, order_id, str(asked["conversation_id"]), str(approval.id))


async def decide(
    client: AsyncClient, approval_id: str, decision: str, reason: str | None = None
) -> tuple[int, dict[str, object]]:
    response = await client.post(
        f"/approvals/{approval_id}",
        json={"decision": decision, "reason": reason},
        headers=await attendant_token(client),
    )
    return response.status_code, response.json()


def notices_of(turn: dict[str, object]) -> list[str]:
    notices = turn["notices"]
    assert isinstance(notices, list)
    return [str(notice) for notice in notices]


async def messages(
    client: AsyncClient, conversation_id: str, customer_id: str
) -> list[dict[str, object]]:
    response = await client.get(
        f"/conversations/{conversation_id}/messages", headers=await token_for(client, customer_id)
    )
    assert response.status_code == 200, response.text
    data: list[dict[str, object]] = response.json()
    return data


async def test_approval_endpoints_need_an_attendant_token(client: AsyncClient) -> None:
    customer = await token_for(client, ANA)
    attendant = await attendant_token(client)

    unknown = await client.post("/auth/dev-token", json={"attendant_id": "att_nobody"})
    both = await client.post("/auth/dev-token", json={"customer_id": ANA, "attendant_id": JULIA})
    anonymous = await client.get("/approvals")
    as_customer = await client.get("/approvals", headers=customer)
    decide_as_customer = await client.post(
        f"/approvals/{uuid.uuid4()}", json={"decision": "approve"}, headers=customer
    )
    chat_as_attendant = await client.post("/chat", json={"message": "oi"}, headers=attendant)

    assert unknown.status_code == 404
    assert both.status_code == 422
    assert anonymous.status_code == 401
    assert as_customer.status_code == 403
    assert decide_as_customer.status_code == 403
    assert chat_as_attendant.status_code == 403


async def test_attendant_lists_pending_requests_with_what_they_need_to_decide(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine, amount_cents=10000)

    response = await client.get(
        "/approvals", params={"status": "pending"}, headers=await attendant_token(client)
    )

    assert response.status_code == 200
    [listed] = [
        item for item in response.json() if item["approval_request_id"] == requested.approval_id
    ]
    assert listed["order_id"] == requested.order_id
    assert listed["customer_id"] == requested.customer_id
    assert listed["amount_cents"] == 10000
    assert listed["reason"]
    assert listed["status"] == "pending"
    assert listed["policy_result"]["ground"] == "delivered_recently"
    assert listed["policy_result"]["max_cents"] == 24990
    assert all(item["status"] == "pending" for item in response.json())


async def test_approval_resumes_the_workflow_issues_the_refund_and_tells_the_customer(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine)

    status, decided = await decide(client, requested.approval_id, "approve")

    [issued] = await refunds_of(engine, requested.order_id)
    [approval] = await approval_requests_of(engine, requested.order_id)
    assert status == 200
    assert (decided["status"], decided["refund_status"]) == ("approved", "issued")
    assert (issued.status, issued.idempotency_key) == ("issued", f"refund:{approval.id}")
    assert (approval.status, approval.decided_by) == ("approved", JULIA)
    role, content = (await stored_messages(engine, requested.conversation_id))[-1]
    assert role == "agent"
    assert f"#{issued.id}" in content and "R$ 100,00" in content and "aprovado" in content
    audit = await audit_of(engine, requested.order_id)
    assert [entry.action for entry in audit] == ["create_refund", "decide_approval", "issue_refund"]
    assert (audit[1].actor_type, audit[1].actor_id) == ("attendant", JULIA)
    assert audit[2].actor_type == "system"


async def test_rejection_needs_a_reason_which_is_relayed_to_the_customer(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine)

    without_reason, _ = await decide(client, requested.approval_id, "reject")
    blank_reason, _ = await decide(client, requested.approval_id, "reject", "   ")
    status, decided = await decide(
        client, requested.approval_id, "reject", "Produto usado fora da garantia"
    )

    [rejected] = await refunds_of(engine, requested.order_id)
    assert (without_reason, blank_reason, status) == (422, 422, 200)
    assert (decided["status"], decided["refund_status"]) == ("rejected", "rejected")
    assert rejected.status == "rejected"
    [*_, decision] = await messages(client, requested.conversation_id, requested.customer_id)
    assert decision["role"] == "agent"
    assert "recusado" in str(decision["content"])
    assert "Produto usado fora da garantia" in str(decision["content"])
    actions = [entry.action for entry in await audit_of(engine, requested.order_id)]
    assert actions == ["create_refund", "decide_approval"]


async def test_deciding_an_already_decided_request_is_a_conflict_without_side_effects(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine)
    await decide(client, requested.approval_id, "approve")
    messages_before = await stored_messages(engine, requested.conversation_id)

    again, _ = await decide(client, requested.approval_id, "approve")
    reversed_, _ = await decide(client, requested.approval_id, "reject", "mudei de ideia")
    unknown, _ = await decide(client, str(uuid.uuid4()), "approve")

    [issued] = await refunds_of(engine, requested.order_id)
    assert (again, reversed_, unknown) == (409, 409, 404)
    assert issued.status == "issued"
    assert await stored_messages(engine, requested.conversation_id) == messages_before
    assert len(await audit_of(engine, requested.order_id)) == 3


async def test_concurrent_decisions_decide_once(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine)

    outcomes = await asyncio.gather(
        decide(client, requested.approval_id, "approve"),
        decide(client, requested.approval_id, "reject", "não"),
    )

    assert sorted(status for status, _ in outcomes) == [200, 409]
    [refund_] = await refunds_of(engine, requested.order_id)
    assert refund_.status in ("issued", "rejected")
    actions = [entry.action for entry in await audit_of(engine, requested.order_id)]
    assert actions.count("decide_approval") == 1


async def test_the_customer_sees_the_decision_on_their_next_message(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine)
    headers = await token_for(client, requested.customer_id)
    await decide(client, requested.approval_id, "approve")
    llm.on("quais são os prazos de entrega?", faq())
    llm.on("e de troca?", faq())

    next_turn = await chat(
        client, headers, "quais são os prazos de entrega?", requested.conversation_id
    )
    after = await chat(client, headers, "e de troca?", requested.conversation_id)

    [notice] = notices_of(next_turn)
    assert "aprovado" in notice
    assert after["notices"] == []


async def test_a_new_conversation_starts_with_unseen_decisions(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine)
    await decide(client, requested.approval_id, "reject", "Pedido fora do prazo de devolução")
    headers = await token_for(client, requested.customer_id)
    llm.on("oi de novo", faq())

    fresh = await chat(client, headers, "oi de novo")
    another = await chat(client, headers, "oi de novo")

    [notice] = notices_of(fresh)
    assert "Pedido fora do prazo de devolução" in notice
    stored = await stored_messages(engine, str(fresh["conversation_id"]))
    assert stored[:2] == [("agent", notice), ("customer", "oi de novo")]
    assert another["notices"] == []
    assert fresh["conversation_id"] != requested.conversation_id


async def test_polling_shows_the_decision_and_marks_it_seen(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine)
    await decide(client, requested.approval_id, "approve")
    llm.on("oi", faq())

    polled = await messages(client, requested.conversation_id, requested.customer_id)
    fresh = await chat(client, await token_for(client, requested.customer_id), "oi")

    assert [m["role"] for m in polled] == ["customer", "agent", "agent"]
    assert "aprovado" in str(polled[-1]["content"])
    assert fresh["notices"] == []


async def test_customer_cannot_poll_another_customers_conversation(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    requested = await refund_requested(client, llm, engine)

    response = await client.get(
        f"/conversations/{requested.conversation_id}/messages",
        headers=await token_for(client, BRUNO),
    )
    unknown = await client.get(
        f"/conversations/{uuid.uuid4()}/messages", headers=await token_for(client, BRUNO)
    )

    assert (response.status_code, unknown.status_code) == (404, 404)


async def test_approval_resumes_the_checkpointed_workflow_after_a_restart(
    settings: Settings, engine: AsyncEngine
) -> None:
    llm = ScriptedLLM()
    async with app_client(settings, llm) as before_restart:
        requested = await refund_requested(before_restart, llm, engine)

    async with app_client(settings, llm) as after_restart:
        status, decided = await decide(after_restart, requested.approval_id, "approve")
        polled = await messages(after_restart, requested.conversation_id, requested.customer_id)

    [issued] = await refunds_of(engine, requested.order_id)
    assert (status, decided["refund_status"], issued.status) == (200, "issued", "issued")
    assert "aprovado" in str(polled[-1]["content"])
