"""Cancellation end to end: /chat pauses for confirmation and the next message resumes it."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.llm import Confirmation
from support_agent.models import OrderStatus

from .fakes import ScriptedLLM, cancel_order, faq
from .test_api import chat, stored_messages, token_for
from .test_cancel_order_tool import audit_of, new_order, order_state

ANA = "cus_ana"


async def ask_to_cancel(
    client: AsyncClient, llm: ScriptedLLM, order_id: str
) -> tuple[dict[str, str], dict[str, object]]:
    headers = await token_for(client, ANA)
    llm.on(f"cancela o pedido {order_id}", cancel_order(order_id))
    return headers, await chat(client, headers, f"cancela o pedido {order_id}")


async def test_confirmed_cancellation_issues_the_refund(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 149900)
    headers, asked = await ask_to_cancel(client, llm, order_id)
    conversation_id = str(asked["conversation_id"])
    yes = f"sim, cancela o {order_id}"
    llm.on_confirmation(yes, Confirmation.CONFIRM)

    done = await chat(client, headers, yes, conversation_id)

    assert asked["intent"] == "cancel_order"
    assert "R$ 1.499,00" in str(asked["reply"])
    assert asked["pending_actions"] == [
        {"type": "confirm_cancellation", "order_id": order_id, "refund_cents": 149900}
    ]
    assert done["pending_actions"] == []
    assert f"cancelei o pedido {order_id}" in str(done["reply"])
    status, _, [refund] = await order_state(engine, order_id)
    assert status is OrderStatus.CANCELLED
    assert (refund.amount_cents, refund.status) == (149900, "issued")
    assert [entry.action for entry in await audit_of(engine, order_id)] == [
        "cancel_order",
        "issue_refund",
    ]
    assert await stored_messages(engine, conversation_id) == [
        ("customer", f"cancela o pedido {order_id}"),
        ("agent", str(asked["reply"])),
        ("customer", yes),
        ("agent", str(done["reply"])),
    ]


async def test_denied_cancellation_keeps_the_order(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    order_id = await new_order(engine, OrderStatus.PROCESSING, 11980)
    headers, asked = await ask_to_cancel(client, llm, order_id)
    no = f"não, mantém o {order_id}"
    llm.on_confirmation(no, Confirmation.DENY)

    kept = await chat(client, headers, no, str(asked["conversation_id"]))

    assert f"o pedido {order_id} continua ativo" in str(kept["reply"])
    assert kept["pending_actions"] == []
    assert await order_state(engine, order_id) == (OrderStatus.PROCESSING, [], [])
    assert await audit_of(engine, order_id) == []


async def test_changing_the_subject_drops_the_cancellation(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    headers, asked = await ask_to_cancel(client, llm, order_id)
    question = f"antes, qual o prazo de reembolso? ({order_id})"
    llm.on(question, faq())

    other = await chat(client, headers, question, str(asked["conversation_id"]))

    assert other["intent"] == "faq"
    assert f"não cancelei o pedido {order_id}" in str(other["reply"])
    assert "Política de Reembolso" in str(other["reply"])
    assert other["pending_actions"] == []
    assert await order_state(engine, order_id) == (OrderStatus.PAID, [], [])


async def test_dispatched_order_is_explained_without_asking(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    _, refused = await ask_to_cancel(client, llm, "ORD-1002")

    assert "ORD-1002 já foi enviado" in str(refused["reply"])
    assert "recusar a entrega" in str(refused["reply"])
    assert refused["pending_actions"] == []


async def test_another_customers_order_is_never_offered_for_cancellation(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    order_id = await new_order(engine, OrderStatus.PAID, 39900, customer_id="cus_bruno")

    _, refused = await ask_to_cancel(client, llm, order_id)

    assert f"Não encontrei o pedido {order_id}" in str(refused["reply"])
    assert refused["pending_actions"] == []
