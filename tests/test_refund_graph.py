"""Refund through the graph: the policy's answer, approval off the Conversation thread, status."""

import uuid
from collections.abc import Callable

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.graph import Turn, run_turn
from support_agent.llm import Classification, Entities, Intent

from .fakes import ScriptedLLM, order_status, refund, status_then_refund
from .test_escalation_graph import Agent, ScriptedCarrier
from .test_refund_tools import approval_requests_of, dispatched_order, refunds_of

ANA = "cus_ana"


def agent(engine: AsyncEngine) -> tuple[Agent, ScriptedLLM]:
    llm = ScriptedLLM()
    return Agent(engine, ScriptedCarrier(), llm), llm


async def say(
    agent: Agent, llm: ScriptedLLM, conversation_id: uuid.UUID, message: str, c: Classification
) -> Turn:
    llm.on(message, c)
    return await run_turn(agent.graph, conversation_id, ANA, message)


async def test_eligible_refund_awaits_approval_without_blocking_the_conversation(
    engine: AsyncEngine,
) -> None:
    ana, llm = agent(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=3, total_paid_cents=24990)
    conversation_id = await ana.new_conversation()

    asked = await say(ana, llm, conversation_id, f"quero reembolso do {order_id}", refund(order_id))
    later = await say(ana, llm, conversation_id, "e onde está o pedido?", order_status())

    [pending] = await refunds_of(engine, order_id)
    assert pending.status == "pending_approval"
    assert pending.reason == f"quero reembolso do {order_id}"
    assert asked.intent == "refund"
    assert "R$ 249,90" in asked.reply
    assert "aguardando aprovação" in asked.reply
    assert f"#{pending.id}" in asked.reply
    # The approval waits on its own thread: nothing pauses the Conversation.
    assert asked.pending_actions == []
    config: RunnableConfig = {"configurable": {"thread_id": str(conversation_id)}}
    assert (await ana.graph.aget_state(config)).interrupts == ()
    # The next message is a new request about the Focused Order, not an answer to anything.
    assert later.intent == "order_status"
    assert f"Pedido {order_id}: entregue." in later.reply
    assert len(await approval_requests_of(engine, order_id)) == 1


async def test_lower_amount_is_requested_as_asked(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=3, total_paid_cents=24990)
    conversation_id = await ana.new_conversation()

    asked = await say(
        ana, llm, conversation_id, "quero R$ 50 de volta", refund(order_id, amount_cents=5000)
    )

    assert "R$ 50,00" in asked.reply
    assert [r.amount_cents for r in await refunds_of(engine, order_id)] == [5000]


async def test_higher_amount_is_capped_and_explained(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=3, total_paid_cents=24990)
    conversation_id = await ana.new_conversation()

    asked = await say(
        ana, llm, conversation_id, "quero R$ 500 de volta", refund(order_id, amount_cents=50000)
    )

    assert "R$ 500,00" in asked.reply
    assert "máximo" in asked.reply
    assert "R$ 249,90" in asked.reply
    assert [r.amount_cents for r in await refunds_of(engine, order_id)] == [24990]


async def test_ineligible_refund_is_explained_citing_the_policy_and_offers_a_ticket(
    engine: AsyncEngine,
) -> None:
    ana, llm = agent(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=45)
    conversation_id = await ana.new_conversation()

    refused = await say(ana, llm, conversation_id, "quero reembolso", refund(order_id))

    assert "Política de Reembolso" in refused.reply
    assert "30 dias" in refused.reply
    assert "chamado" in refused.reply
    assert await refunds_of(engine, order_id) == []


async def test_repeated_request_says_the_refund_is_already_awaiting_approval(
    engine: AsyncEngine,
) -> None:
    ana, llm = agent(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=3, total_paid_cents=24990)
    conversation_id = await ana.new_conversation()
    await say(ana, llm, conversation_id, "quero reembolso", refund(order_id))

    again = await say(ana, llm, conversation_id, "quero reembolso de novo", refund())

    [pending] = await refunds_of(engine, order_id)
    assert "já tem um reembolso" in again.reply
    assert f"#{pending.id}" in again.reply


async def test_refund_status_answers_from_the_refund_records(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=3, total_paid_cents=24990)
    conversation_id = await ana.new_conversation()
    await say(ana, llm, conversation_id, "quero reembolso", refund(order_id))

    status = await say(
        ana, llm, conversation_id, "status do meu reembolso", refund(order_id, status=True)
    )

    [pending] = await refunds_of(engine, order_id)
    assert status.intent == "refund"
    assert f"#{pending.id}" in status.reply
    assert order_id in status.reply
    assert "R$ 249,90" in status.reply
    assert "aguardando aprovação" in status.reply
    assert len(await refunds_of(engine, order_id)) == 1


async def test_refund_status_without_refunds_says_so(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=3)
    conversation_id = await ana.new_conversation()

    status = await say(ana, llm, conversation_id, "e o reembolso?", refund(order_id, status=True))

    assert f"Não encontrei reembolsos do pedido {order_id}" in status.reply


async def test_queued_refund_keeps_the_amount_asked_for(engine: AsyncEngine) -> None:
    ana, llm = agent(engine)
    order_id = await dispatched_order(engine, delivered_days_ago=3, total_paid_cents=24990)
    conversation_id = await ana.new_conversation()
    both = Classification(
        intents=[Intent.ORDER_STATUS, Intent.REFUND],
        confidence=0.95,
        entities=Entities(order_id=order_id, requested_amount_cents=5000),
    )
    await say(ana, llm, conversation_id, f"cadê o {order_id}? e quero R$ 50 de volta", both)

    resumed = await run_turn(ana.graph, conversation_id, ANA, "pode seguir")

    assert resumed.intent == "refund"
    assert "R$ 50,00" in resumed.reply
    assert [r.amount_cents for r in await refunds_of(engine, order_id)] == [5000]


async def status_and_refund_queued(
    engine: AsyncEngine, c: Callable[[str, str], Classification]
) -> tuple[Agent, ScriptedLLM, uuid.UUID, Turn, str, str]:
    """Ana asks where one Order is and for a Refund of another; the Refund is queued."""
    ana, llm = agent(engine)
    asked_about = await dispatched_order(engine)
    to_refund = await dispatched_order(engine, delivered_days_ago=3)
    conversation_id = await ana.new_conversation()
    message = f"cadê o {asked_about}? e quero reembolso do {to_refund}"
    first = await say(ana, llm, conversation_id, message, c(asked_about, to_refund))
    return ana, llm, conversation_id, first, asked_about, to_refund


async def test_queued_refund_is_for_the_order_named_with_it(engine: AsyncEngine) -> None:
    ana, _, conversation_id, first, asked_about, to_refund = await status_and_refund_queued(
        engine, status_then_refund
    )

    resumed = await run_turn(ana.graph, conversation_id, ANA, "pode seguir")

    assert f"Pedido {asked_about}" in first.reply
    assert resumed.intent == "refund"
    assert len(await approval_requests_of(engine, to_refund)) == 1
    assert await approval_requests_of(engine, asked_about) == []


async def test_asking_again_for_the_queued_refund_carries_on_with_it(engine: AsyncEngine) -> None:
    ana, llm, conversation_id, _, asked_about, to_refund = await status_and_refund_queued(
        engine, lambda status, refund: status_then_refund(status, refund, amount_cents=5000)
    )

    resumed = await say(ana, llm, conversation_id, "ok, pode seguir com o reembolso", refund())

    assert resumed.intent == "refund"
    assert [r.amount_cents for r in await refunds_of(engine, to_refund)] == [5000]
    assert await approval_requests_of(engine, asked_about) == []


async def test_an_intent_without_its_own_order_does_not_take_the_next_ones(
    engine: AsyncEngine,
) -> None:
    _, _, _, first, _, to_refund = await status_and_refund_queued(
        engine, lambda _, refund: status_then_refund(None, refund, order_id=refund)
    )

    assert first.intent == "order_status"
    assert f"Pedido {to_refund}" not in first.reply


async def test_orders_not_matching_the_intents_fall_back_to_the_single_order(
    engine: AsyncEngine,
) -> None:
    _, _, _, first, asked_about, _ = await status_and_refund_queued(
        engine,
        lambda status, _: Classification(
            intents=[Intent.ORDER_STATUS, Intent.REFUND],
            confidence=0.95,
            entities=Entities(order_id=status, order_ids=[status]),
        ),
    )

    assert f"Pedido {asked_about}" in first.reply
