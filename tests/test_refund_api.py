"""Refund end to end: /chat creates the Approval Request, whose workflow pauses in Postgres."""

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.approvals import approval_thread_id

from .fakes import ScriptedLLM, order_status, refund
from .test_api import chat, token_for
from .test_cancel_order_tool import audit_of
from .test_refund_tools import approval_requests_of, dispatched_order, refunds_of

ANA = "cus_ana"


async def checkpointed_interrupts(engine: AsyncEngine, thread_id: str) -> int:
    """Pending interrupt writes the Postgres checkpointer holds for the thread."""
    async with engine.connect() as connection:
        count = await connection.scalar(
            text(
                "SELECT count(*) FROM checkpoint_writes "
                "WHERE thread_id = :thread_id AND channel = '__interrupt__'"
            ),
            {"thread_id": thread_id},
        )
    return int(count or 0)


async def test_refund_request_pauses_its_approval_workflow_while_the_customer_chats_on(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    order_id = await dispatched_order(engine, delivered_days_ago=4, total_paid_cents=24990)
    headers = await token_for(client, ANA)
    llm.on(f"quero reembolso do {order_id}", refund(order_id, amount_cents=10000))
    llm.on(f"status do reembolso do {order_id}", refund(order_id, status=True))
    llm.on(f"e o {order_id}, onde está?", order_status(order_id))

    asked = await chat(client, headers, f"quero reembolso do {order_id}")
    conversation_id = str(asked["conversation_id"])
    status = await chat(client, headers, f"status do reembolso do {order_id}", conversation_id)
    later = await chat(client, headers, f"e o {order_id}, onde está?", conversation_id)

    [pending] = await refunds_of(engine, order_id)
    [approval] = await approval_requests_of(engine, order_id)
    assert (pending.status, pending.amount_cents) == ("pending_approval", 10000)
    assert (approval.status, str(approval.conversation_id)) == ("pending", conversation_id)
    assert asked["intent"] == "refund"
    assert asked["pending_actions"] == []
    assert "R$ 100,00" in str(asked["reply"])
    assert "aguardando aprovação" in str(asked["reply"])
    assert await checkpointed_interrupts(engine, approval_thread_id(approval.id)) == 1
    assert f"#{pending.id}" in str(status["reply"])
    assert "aguardando aprovação" in str(status["reply"])
    assert later["intent"] == "order_status"
    assert f"Pedido {order_id}: entregue." in str(later["reply"])
    assert [entry.action for entry in await audit_of(engine, order_id)] == ["create_refund"]
