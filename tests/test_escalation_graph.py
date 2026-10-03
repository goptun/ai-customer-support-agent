"""Escalation through the graph with the scripted fake LLM and a scripted carrier."""

import uuid
from typing import Any

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.db import session_factory
from support_agent.graph import AgentDeps, RunContext, build_graph, run_turn
from support_agent.llm import Classification, Entities, Intent
from support_agent.logistics import LogisticsClient
from support_agent.models import Conversation, Ticket
from support_agent.orders import Orders
from support_agent.seed import ANCHOR
from support_agent.tickets import Tickets
from support_agent.tools import RetryPolicy, ToolDeps

from .fakes import ScriptedLLM, order_status
from .test_order_status_tool import tracking_json

ANA = "cus_ana"


def wants_human(order_id: str | None = None) -> Classification:
    return Classification(
        intents=[Intent.OTHER],
        confidence=0.9,
        entities=Entities(order_id=order_id, wants_human=True),
    )


class ScriptedCarrier:
    """Fails the next `failures` requests with a 503, then answers in transit."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            return httpx.Response(503)
        code = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=tracking_json(code, "in_transit"))


class SpyLLM(ScriptedLLM):
    def __init__(self) -> None:
        super().__init__()
        self.responded = 0

    async def respond(self, history: Any, facts: str) -> str:
        self.responded += 1
        return await super().respond(history, facts)


class Agent:
    def __init__(self, engine: AsyncEngine, carrier: ScriptedCarrier, llm: ScriptedLLM) -> None:
        self.engine = engine
        sessions = session_factory(engine)
        http = httpx.AsyncClient(
            base_url="http://logistics.test", transport=httpx.MockTransport(carrier)
        )
        self.graph: CompiledStateGraph[Any, RunContext] = build_graph(
            InMemorySaver(),
            AgentDeps(
                llm=llm,
                tools=ToolDeps(
                    orders=Orders(sessions),
                    logistics=LogisticsClient(http, timeout_seconds=0.5),
                    tickets=Tickets(sessions),
                    today=ANCHOR.date,
                ),
                retry=RetryPolicy(retries=2, backoff_seconds=0),
                failures_before_escalation=2,
            ),
        )

    async def new_conversation(self, customer_id: str = ANA) -> uuid.UUID:
        async with session_factory(self.engine).begin() as session:
            conversation = Conversation(customer_id=customer_id)
            session.add(conversation)
            await session.flush()
            return conversation.id

    async def say(self, conversation_id: uuid.UUID, message: str, customer_id: str = ANA) -> str:
        return (await run_turn(self.graph, conversation_id, customer_id, message)).reply

    async def tickets(self, conversation_id: uuid.UUID) -> list[Ticket]:
        async with session_factory(self.engine)() as session:
            return list(
                (
                    await session.scalars(
                        select(Ticket).where(Ticket.conversation_id == conversation_id)
                    )
                ).all()
            )


async def test_asking_for_a_human_opens_a_ticket_immediately(engine: AsyncEngine) -> None:
    llm, carrier = SpyLLM(), ScriptedCarrier()
    agent = Agent(engine, carrier, llm)
    llm.on("quero falar com um atendente sobre o 1002", wants_human("1002"))
    conversation_id = await agent.new_conversation()

    reply = await agent.say(conversation_id, "quero falar com um atendente sobre o 1002")

    [ticket] = await agent.tickets(conversation_id)
    assert ticket.reason == "customer_requested"
    assert ticket.priority == "normal"
    assert "quero falar com um atendente sobre o 1002" in ticket.summary
    assert f"#{ticket.id}" in reply
    assert "atendente" in reply
    # No retention attempt: no tool call, no LLM-written reply.
    assert carrier.calls == 0
    assert llm.responded == 0


async def test_ticket_carries_the_order_already_looked_up(engine: AsyncEngine) -> None:
    llm = ScriptedLLM()
    agent = Agent(engine, ScriptedCarrier(), llm)
    llm.on("cadê o 1002?", order_status("1002"))
    llm.on("quero um humano", wants_human())
    conversation_id = await agent.new_conversation()

    await agent.say(conversation_id, "cadê o 1002?")
    await agent.say(conversation_id, "quero um humano")

    [ticket] = await agent.tickets(conversation_id)
    assert ticket.order_id == "ORD-1002"
    assert "cadê o 1002?" in ticket.summary


async def test_unverified_order_is_not_attached_to_the_ticket(engine: AsyncEngine) -> None:
    llm = ScriptedLLM()
    agent = Agent(engine, ScriptedCarrier(), llm)
    llm.on("humano agora, pedido 2002", wants_human("2002"))  # Bruno's Order
    conversation_id = await agent.new_conversation()

    await agent.say(conversation_id, "humano agora, pedido 2002")

    [ticket] = await agent.tickets(conversation_id)
    assert ticket.order_id is None


async def test_asking_twice_keeps_a_single_ticket(engine: AsyncEngine) -> None:
    llm = ScriptedLLM()
    agent = Agent(engine, ScriptedCarrier(), llm)
    llm.on("quero um humano", wants_human())
    llm.on("cadê o humano??", wants_human())
    conversation_id = await agent.new_conversation()

    first = await agent.say(conversation_id, "quero um humano")
    second = await agent.say(conversation_id, "cadê o humano??")

    [ticket] = await agent.tickets(conversation_id)
    assert f"#{ticket.id}" in first and f"#{ticket.id}" in second


async def test_each_tool_call_retries_twice_before_failing(engine: AsyncEngine) -> None:
    llm, carrier = ScriptedLLM(), ScriptedCarrier(failures=2)
    agent = Agent(engine, carrier, llm)
    llm.on("cadê o 1002?", order_status("1002"))
    conversation_id = await agent.new_conversation()

    reply = await agent.say(conversation_id, "cadê o 1002?")

    assert carrier.calls == 3
    assert "em trânsito" in reply
    assert await agent.tickets(conversation_id) == []


async def test_one_failed_call_is_reported_without_escalating(engine: AsyncEngine) -> None:
    llm, carrier = ScriptedLLM(), ScriptedCarrier(failures=100)
    agent = Agent(engine, carrier, llm)
    llm.on("cadê o 1002?", order_status("1002"))
    conversation_id = await agent.new_conversation()

    reply = await agent.say(conversation_id, "cadê o 1002?")

    assert carrier.calls == 3
    assert "não consegui consultar a transportadora" in reply.lower()
    assert await agent.tickets(conversation_id) == []


async def test_second_failed_call_of_the_same_tool_escalates(engine: AsyncEngine) -> None:
    llm, carrier = SpyLLM(), ScriptedCarrier(failures=100)
    agent = Agent(engine, carrier, llm)
    llm.on("cadê o 1002?", order_status("1002"))
    llm.on("e agora, o 1002?", order_status("1002"))
    conversation_id = await agent.new_conversation()

    await agent.say(conversation_id, "cadê o 1002?")
    reply = await agent.say(conversation_id, "e agora, o 1002?")

    assert carrier.calls == 6
    [ticket] = await agent.tickets(conversation_id)
    assert ticket.reason == "tool_failure"
    assert ticket.priority == "high"
    assert ticket.order_id == "ORD-1002"
    assert "get_order_status" in ticket.summary
    assert f"#{ticket.id}" in reply
    assert "em trânsito" not in reply


async def test_failures_do_not_add_up_across_conversations(engine: AsyncEngine) -> None:
    llm = ScriptedLLM()
    agent = Agent(engine, ScriptedCarrier(failures=100), llm)
    llm.on("cadê o 1002?", order_status("1002"))
    first, second = await agent.new_conversation(), await agent.new_conversation()

    await agent.say(first, "cadê o 1002?")
    await agent.say(second, "cadê o 1002?")

    assert await agent.tickets(first) == []
    assert await agent.tickets(second) == []


async def test_long_messages_still_fit_in_the_ticket_summary(engine: AsyncEngine) -> None:
    llm = ScriptedLLM()
    agent = Agent(engine, ScriptedCarrier(), llm)
    rant = "quero um humano " + "x" * 3980
    llm.on(rant, wants_human())
    conversation_id = await agent.new_conversation()

    for _ in range(5):
        reply = await agent.say(conversation_id, rant)

    [ticket] = await agent.tickets(conversation_id)
    assert f"#{ticket.id}" in reply
