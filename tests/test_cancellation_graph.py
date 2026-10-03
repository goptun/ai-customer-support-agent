"""Cancellation through the graph: explicit confirmation via interrupt, then confirm/deny/other."""

import uuid

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.graph import Turn, run_turn
from support_agent.llm import Confirmation
from support_agent.models import OrderStatus

from .fakes import ScriptedLLM, cancel_order, order_status
from .test_cancel_order_tool import new_order, order_state
from .test_escalation_graph import Agent, ScriptedCarrier

ANA = "cus_ana"


def agent(engine: AsyncEngine) -> tuple[Agent, ScriptedLLM]:
    llm = ScriptedLLM()
    return Agent(engine, ScriptedCarrier(), llm), llm


async def turn(agent: Agent, conversation_id: uuid.UUID, message: str) -> Turn:
    return await run_turn(agent.graph, conversation_id, ANA, message)


async def ask_to_cancel(agent: Agent, llm: ScriptedLLM, order_id: str) -> tuple[uuid.UUID, Turn]:
    conversation_id = await agent.new_conversation()
    llm.on(f"quero cancelar o {order_id}", cancel_order(order_id))
    return conversation_id, await turn(agent, conversation_id, f"quero cancelar o {order_id}")


async def test_asks_for_confirmation_stating_order_and_refund(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.PAID, 39900)

    _, asked = await ask_to_cancel(ana, llm, order_id)

    assert order_id in asked.reply
    assert "R$ 399,00" in asked.reply
    assert asked.intent == "cancel_order"
    assert asked.pending_actions == [
        {"type": "confirm_cancellation", "order_id": order_id, "refund_cents": 39900}
    ]
    assert (await order_state(engine, order_id))[0] is OrderStatus.PAID


async def test_unpaid_order_confirmation_says_nothing_is_refunded(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.PENDING_PAYMENT, 0)

    _, asked = await ask_to_cancel(ana, llm, order_id)

    assert "não há valor a reembolsar" in asked.reply
    assert "R$" not in asked.reply


async def test_confirm_cancels_and_issues_the_refund(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    conversation_id, _ = await ask_to_cancel(ana, llm, order_id)
    llm.on_confirmation("sim, pode cancelar", Confirmation.CONFIRM)

    done = await turn(ana, conversation_id, "sim, pode cancelar")

    status, _, [refund] = await order_state(engine, order_id)
    assert status is OrderStatus.CANCELLED
    assert f"cancelei o pedido {order_id}" in done.reply
    assert "R$ 399,00" in done.reply
    assert f"#{refund.id}" in done.reply
    assert done.pending_actions == []


async def test_deny_keeps_the_order(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    conversation_id, _ = await ask_to_cancel(ana, llm, order_id)
    llm.on_confirmation("não, deixa", Confirmation.DENY)

    kept = await turn(ana, conversation_id, "não, deixa")

    assert await order_state(engine, order_id) == (OrderStatus.PAID, [], [])
    assert f"o pedido {order_id} continua ativo" in kept.reply
    assert kept.pending_actions == []


async def test_anything_else_drops_the_cancellation_and_is_handled_as_new(
    engine: AsyncEngine,
) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    conversation_id, _ = await ask_to_cancel(ana, llm, order_id)
    llm.on("e o 1002, onde está?", order_status("1002"))

    other = await turn(ana, conversation_id, "e o 1002, onde está?")

    assert await order_state(engine, order_id) == (OrderStatus.PAID, [], [])
    assert f"não cancelei o pedido {order_id}" in other.reply
    assert "ORD-1002" in other.reply and "em trânsito" in other.reply
    assert other.intent == "order_status"
    assert other.pending_actions == []


async def test_a_yes_after_the_cancellation_was_dropped_cancels_nothing(
    engine: AsyncEngine,
) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    conversation_id, _ = await ask_to_cancel(ana, llm, order_id)
    llm.on_confirmation("sim", Confirmation.CONFIRM)

    await turn(ana, conversation_id, "hmm, quanto tempo leva o reembolso?")
    await turn(ana, conversation_id, "sim")

    assert (await order_state(engine, order_id))[0] is OrderStatus.PAID


async def test_dispatched_order_gets_explanation_and_next_steps(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.SHIPPED, 18900)

    _, refused = await ask_to_cancel(ana, llm, order_id)

    assert f"pedido {order_id} já foi enviado" in refused.reply
    assert "recusar a entrega" in refused.reply
    assert "devolução" in refused.reply
    assert refused.pending_actions == []


async def test_already_cancelled_order_is_not_asked_again(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.CANCELLED, 8990)

    _, refused = await ask_to_cancel(ana, llm, order_id)

    assert f"pedido {order_id} já está cancelado" in refused.reply
    assert refused.pending_actions == []


async def test_confirmation_survives_a_restart(engine: AsyncEngine) -> None:
    checkpointer = InMemorySaver()
    llm = ScriptedLLM()
    before = Agent(engine, ScriptedCarrier(), llm, checkpointer)
    order_id = await new_order(engine, OrderStatus.PROCESSING, 11980)
    conversation_id, _ = await ask_to_cancel(before, llm, order_id)
    llm.on_confirmation("confirmo", Confirmation.CONFIRM)

    after = Agent(engine, ScriptedCarrier(), llm, checkpointer)
    done = await turn(after, conversation_id, "confirmo")

    assert (await order_state(engine, order_id))[0] is OrderStatus.CANCELLED
    assert "R$ 119,80" in done.reply


async def test_cancelling_without_an_order_id_uses_the_focused_order(
    engine: AsyncEngine,
) -> None:
    ana, llm = agent(engine)
    order_id = await new_order(engine, OrderStatus.PAID, 39900)
    conversation_id = await ana.new_conversation()
    llm.on(f"cadê o {order_id}?", order_status(order_id))
    llm.on("quero cancelar ele", cancel_order())

    await turn(ana, conversation_id, f"cadê o {order_id}?")
    asked = await turn(ana, conversation_id, "quero cancelar ele")

    assert asked.pending_actions == [
        {"type": "confirm_cancellation", "order_id": order_id, "refund_cents": 39900}
    ]
