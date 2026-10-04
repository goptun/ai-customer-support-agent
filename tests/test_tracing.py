"""LangSmith tracing of a Conversation turn, switched on by the standard env vars."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import Client, tracing_context
from langsmith.utils import get_env_var
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.approvals import (
    APPROVAL_RUN_NAME,
    ApprovalDecision,
    ApprovalPause,
    ApprovalWorkflow,
    build_approval_graph,
)
from support_agent.graph import TURN_RUN_NAME

from .fakes import ScriptedLLM, order_status
from .test_escalation_graph import Agent, ScriptedCarrier


class RecordingLangSmith(Client):
    """A LangSmith client that keeps the runs it would send instead of calling the API."""

    def __init__(self) -> None:
        super().__init__(api_key="fake", api_url="http://langsmith.test", auto_batch_tracing=False)
        self.created: dict[str, dict[str, Any]] = {}
        self.updated: dict[str, dict[str, Any]] = {}

    def create_run(self, *args: Any, **kwargs: Any) -> None:
        self.created[str(kwargs["id"])] = kwargs

    def update_run(self, *args: Any, **kwargs: Any) -> None:
        self.updated[str(kwargs["run_id"])] = kwargs

    def named(self, name: str) -> list[dict[str, Any]]:
        return [run for run in self.created.values() if run["name"] == name]


@pytest.fixture
def tracing_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """LangSmith reads its env vars once per process; forget them around each test."""
    forget = get_env_var.cache_clear  # type: ignore[attr-defined]  # an lru_cache under overloads
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    forget()
    yield monkeypatch
    forget()


@contextmanager
def recording() -> Iterator[RecordingLangSmith]:
    client = RecordingLangSmith()
    with tracing_context(client=client):
        yield client


async def ask_for_order_1002(engine: AsyncEngine) -> uuid.UUID:
    """One Customer turn on a new Conversation; returns the Conversation id."""
    llm = ScriptedLLM()
    agent = Agent(engine, ScriptedCarrier(), llm)
    llm.on("cadê o 1002?", order_status("1002"))
    conversation_id = await agent.new_conversation()
    await agent.say(conversation_id, "cadê o 1002?")
    return conversation_id


async def test_each_node_of_a_turn_is_traced_under_the_conversation(
    engine: AsyncEngine, tracing_env: pytest.MonkeyPatch
) -> None:
    tracing_env.setenv("LANGSMITH_TRACING", "true")
    with recording() as langsmith:
        conversation_id = await ask_for_order_1002(engine)

    [turn] = langsmith.named(TURN_RUN_NAME)
    assert turn["extra"]["metadata"]["conversation_id"] == str(conversation_id)
    for node in ("classify_intent", "resolve_context", "act", "respond"):
        [span] = langsmith.named(node)
        assert span["parent_run_id"] == turn["id"]
        assert span["inputs"]["messages"]
        finished = langsmith.updated[str(span["id"])]
        assert finished["end_time"] >= span["start_time"]
    respond = langsmith.updated[str(langsmith.named("respond")[0]["id"])]
    assert "ORD-1002" in respond["outputs"]["messages"][-1].content


async def test_tool_calls_are_traced_with_their_args_and_result(
    engine: AsyncEngine, tracing_env: pytest.MonkeyPatch
) -> None:
    tracing_env.setenv("LANGSMITH_TRACING", "true")
    with recording() as langsmith:
        await ask_for_order_1002(engine)

    [act] = langsmith.named("act")
    [tool] = langsmith.named("get_order_status")
    assert tool["run_type"] == "tool"
    assert tool["parent_run_id"] == act["id"]
    assert tool["inputs"] == {"args": {"order_id": "1002"}}
    assert langsmith.updated[str(tool["id"])]["outputs"]["order_id"] == "ORD-1002"


async def test_nothing_is_traced_without_the_env_vars(
    engine: AsyncEngine, tracing_env: pytest.MonkeyPatch
) -> None:
    with recording() as langsmith:
        await ask_for_order_1002(engine)

    assert langsmith.created == {}


async def test_an_approval_request_is_traced_by_its_id(tracing_env: pytest.MonkeyPatch) -> None:
    tracing_env.setenv("LANGSMITH_TRACING", "true")
    settled: list[uuid.UUID] = []

    async def settle(approval_request_id: uuid.UUID) -> None:
        settled.append(approval_request_id)

    workflow = ApprovalWorkflow(build_approval_graph(InMemorySaver(), settle))
    pause = ApprovalPause(
        approval_request_id=uuid.uuid4(), refund_id=1, order_id="ORD-1001", amount_cents=100
    )

    with recording() as langsmith:
        await workflow.start(pause)
        await workflow.resume(
            pause.approval_request_id,
            ApprovalDecision(approved=True, attendant_id="att_julia", reason=None),
        )

    started, decided = langsmith.named(APPROVAL_RUN_NAME)
    for run in (started, decided):
        assert run["extra"]["metadata"]["approval_request_id"] == str(pause.approval_request_id)
    [settle_span] = langsmith.named("settle")
    assert settle_span["parent_run_id"] == decided["id"]
    assert settled == [pause.approval_request_id]
