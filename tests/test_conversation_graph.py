"""Focused Order, Clarification and multiple Intents through the graph with the fake LLM."""

from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.llm import Classification, Entities, Intent

from .fakes import ScriptedLLM, order_status
from .test_escalation_graph import Agent, ScriptedCarrier

DIEGO = "cus_diego"  # a single recent Order: ORD-4001


def unsure(order_id: str | None = None) -> Classification:
    return Classification(
        intents=[Intent.ORDER_STATUS], confidence=0.5, entities=Entities(order_id=order_id)
    )


def intents(*values: Intent, order_id: str | None = None) -> Classification:
    return Classification(
        intents=list(values), confidence=0.9, entities=Entities(order_id=order_id)
    )


def agent(engine: AsyncEngine) -> tuple[Agent, ScriptedLLM, ScriptedCarrier]:
    llm, carrier = ScriptedLLM(), ScriptedCarrier()
    return Agent(engine, carrier, llm), llm, carrier


async def test_without_an_order_id_recent_orders_are_listed(engine: AsyncEngine) -> None:
    ana, llm, carrier = agent(engine)
    llm.on("cadê meu pedido?", order_status())
    conversation_id = await ana.new_conversation()

    reply = await ana.say(conversation_id, "cadê meu pedido?")

    listed = [order for order in ("ORD-1003", "ORD-1002", "ORD-1004", "ORD-1001") if order in reply]
    assert listed == ["ORD-1003", "ORD-1002", "ORD-1004", "ORD-1001"]
    assert sorted(listed, key=reply.index) == listed  # newest first
    assert "ORD-2" not in reply
    assert carrier.calls == 0


async def test_order_picked_from_the_list_stays_focused(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    llm.on("cadê meu pedido?", order_status())
    llm.on("o 1002", order_status("1002"))
    llm.on("e quando chega?", order_status())
    conversation_id = await ana.new_conversation()

    await ana.say(conversation_id, "cadê meu pedido?")
    picked = await ana.say(conversation_id, "o 1002")
    follow_up = await ana.say(conversation_id, "e quando chega?")

    assert "em trânsito" in picked
    assert "Pedido ORD-1002" in follow_up
    assert "Previsão de entrega: 03/09/2026" in follow_up


async def test_a_single_recent_order_is_confirmed_without_a_list(engine: AsyncEngine) -> None:
    diego, llm, _ = agent(engine)
    llm.on("meu pedido já saiu?", order_status())
    llm.on("e quando chega?", order_status())
    conversation_id = await diego.new_conversation(DIEGO)

    reply = await diego.say(conversation_id, "meu pedido já saiu?", DIEGO)
    follow_up = await diego.say(conversation_id, "e quando chega?", DIEGO)

    assert "ORD-4001" in reply
    assert "em separação" in reply
    assert "Pedido ORD-4001" in follow_up


async def test_mentioning_another_order_switches_the_focus(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    llm.on("cadê o 1002?", order_status("1002"))
    llm.on("e o 1003?", order_status("1003"))
    llm.on("e quando chega?", order_status())
    conversation_id = await ana.new_conversation()

    await ana.say(conversation_id, "cadê o 1002?")
    switched = await ana.say(conversation_id, "e o 1003?")
    follow_up = await ana.say(conversation_id, "e quando chega?")

    assert "Pedido ORD-1003" in switched
    assert "Pedido ORD-1003" in follow_up


async def test_someone_elses_order_does_not_take_the_focus(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    llm.on("cadê o 1002?", order_status("1002"))
    llm.on("e o 2002?", order_status("2002"))  # Bruno's Order
    llm.on("e quando chega?", order_status())
    conversation_id = await ana.new_conversation()

    await ana.say(conversation_id, "cadê o 1002?")
    await ana.say(conversation_id, "e o 2002?")
    follow_up = await ana.say(conversation_id, "e quando chega?")

    assert "Pedido ORD-1002" in follow_up


async def test_an_unsure_classification_asks_a_clarification(engine: AsyncEngine) -> None:
    ana, llm, carrier = agent(engine)
    llm.on("hmm o 1002 aquele lá", unsure("1002"))
    conversation_id = await ana.new_conversation()

    reply = await ana.say(conversation_id, "hmm o 1002 aquele lá")

    assert "não tenho certeza se entendi" in reply.lower()
    assert carrier.calls == 0
    assert await ana.tickets(conversation_id) == []


async def test_two_failed_clarifications_open_a_ticket(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    for message in ("hmm", "sei lá", "aquilo"):
        llm.on(message, unsure())
    conversation_id = await ana.new_conversation()

    await ana.say(conversation_id, "hmm")
    await ana.say(conversation_id, "sei lá")
    assert await ana.tickets(conversation_id) == []
    reply = await ana.say(conversation_id, "aquilo")

    [ticket] = await ana.tickets(conversation_id)
    assert ticket.reason == "clarification_failed"
    assert ticket.priority == "normal"
    assert "aquilo" in ticket.summary
    assert f"#{ticket.id}" in reply


async def test_an_understood_message_resets_the_clarifications(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    for message in ("hmm", "sei lá", "aquilo"):
        llm.on(message, unsure())
    llm.on("cadê o 1002?", order_status("1002"))
    conversation_id = await ana.new_conversation()

    await ana.say(conversation_id, "hmm")
    await ana.say(conversation_id, "sei lá")
    await ana.say(conversation_id, "cadê o 1002?")
    reply = await ana.say(conversation_id, "aquilo")

    assert "não tenho certeza se entendi" in reply.lower()
    assert await ana.tickets(conversation_id) == []


async def test_a_second_intent_is_announced_and_handled_next(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    message = "qual o prazo de troca? e cadê o 1002?"
    llm.on(message, intents(Intent.FAQ, Intent.ORDER_STATUS, order_id="1002"))
    conversation_id = await ana.new_conversation()

    first = await ana.say(conversation_id, message)
    second = await ana.say(conversation_id, "ok")  # unscripted: `other`
    third = await ana.say(conversation_id, "ok")

    assert "status do pedido" in first
    assert "em trânsito" not in first
    assert "Pedido ORD-1002" in second
    assert "em trânsito" in second
    assert "Pedido ORD-1002" not in third


async def test_greeting_alongside_a_request_is_not_handled_first(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    llm.on("oi! cadê o 1002?", intents(Intent.OTHER, Intent.ORDER_STATUS, order_id="1002"))
    conversation_id = await ana.new_conversation()

    reply = await ana.say(conversation_id, "oi! cadê o 1002?")

    assert "em trânsito" in reply
    assert "Também vi" not in reply


async def test_small_talk_gets_what_the_agent_can_do(engine: AsyncEngine) -> None:
    ana, _, carrier = agent(engine)
    conversation_id = await ana.new_conversation()

    reply = await ana.say(conversation_id, "qual a capital da França?")  # unscripted: `other`

    assert "acompanhar seus pedidos" in reply
    assert carrier.calls == 0
    assert await ana.tickets(conversation_id) == []


async def test_asking_for_the_queued_intent_directly_takes_it_off_the_queue(
    engine: AsyncEngine,
) -> None:
    ana, llm, _ = agent(engine)
    message = "qual o prazo de troca? e cadê o 1002?"
    llm.on(message, intents(Intent.FAQ, Intent.ORDER_STATUS, order_id="1002"))
    llm.on("cadê o 1002?", order_status("1002"))
    conversation_id = await ana.new_conversation()

    await ana.say(conversation_id, message)
    asked = await ana.say(conversation_id, "cadê o 1002?")
    after = await ana.say(conversation_id, "ok")

    assert "em trânsito" in asked
    assert "Também vi" not in asked
    assert "Pedido ORD-1002" not in after


async def test_the_queue_waits_while_the_customer_asks_something_new(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    message = "qual o prazo de troca? e cadê o 1002?"
    llm.on(message, intents(Intent.FAQ, Intent.ORDER_STATUS, order_id="1002"))
    llm.on("quero cancelar", intents(Intent.CANCEL_ORDER))
    conversation_id = await ana.new_conversation()

    await ana.say(conversation_id, message)
    meanwhile = await ana.say(conversation_id, "quero cancelar")
    resumed = await ana.say(conversation_id, "ok")

    assert "status do pedido" in meanwhile
    assert "Pedido ORD-1002" in resumed


async def test_asking_for_a_human_ends_the_run_of_clarifications(engine: AsyncEngine) -> None:
    ana, llm, _ = agent(engine)
    for message in ("hmm", "sei lá", "aquilo"):
        llm.on(message, unsure())
    llm.on(
        "quero um humano",
        Classification(intents=[Intent.OTHER], confidence=0.9, entities=Entities(wants_human=True)),
    )
    conversation_id = await ana.new_conversation()

    await ana.say(conversation_id, "hmm")
    await ana.say(conversation_id, "sei lá")
    await ana.say(conversation_id, "quero um humano")
    reply = await ana.say(conversation_id, "aquilo")

    assert "não tenho certeza se entendi" in reply.lower()
    [ticket] = await ana.tickets(conversation_id)
    assert ticket.reason == "customer_requested"
