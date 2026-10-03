"""LangSmith tracing of a Conversation turn, switched on by the standard env vars."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from langsmith import Client, tracing_context
from langsmith.utils import get_env_var
from sqlalchemy.ext.asyncio import AsyncEngine

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
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    get_env_var.cache_clear()  # type: ignore[attr-defined]  # an lru_cache under overloads
    yield monkeypatch
    get_env_var.cache_clear()  # type: ignore[attr-defined]  # an lru_cache under overloads


@contextmanager
def recording() -> Iterator[RecordingLangSmith]:
    client = RecordingLangSmith()
    with tracing_context(client=client):
        yield client


async def test_each_node_of_a_turn_is_traced_under_the_conversation(
    engine: AsyncEngine, tracing_env: pytest.MonkeyPatch
) -> None:
    tracing_env.setenv("LANGSMITH_TRACING", "true")
    llm = ScriptedLLM()
    agent = Agent(engine, ScriptedCarrier(), llm)
    llm.on("cadê o 1002?", order_status("1002"))
    conversation_id = await agent.new_conversation()

    with recording() as langsmith:
        await agent.say(conversation_id, "cadê o 1002?")

    [turn] = langsmith.named("conversation_turn")
    assert turn["extra"]["metadata"]["conversation_id"] == str(conversation_id)
    for node in ("classify_intent", "resolve_context", "act", "respond"):
        [span] = langsmith.named(node)
        assert span["parent_run_id"] == turn["id"]
        assert span["inputs"]["messages"]
        finished = langsmith.updated[str(span["id"])]
        assert finished["end_time"] >= span["start_time"]
    respond = langsmith.updated[str(langsmith.named("respond")[0]["id"])]
    assert "ORD-1002" in respond["outputs"]["messages"][-1].content


async def test_nothing_is_traced_without_the_env_vars(
    engine: AsyncEngine, tracing_env: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLLM()
    agent = Agent(engine, ScriptedCarrier(), llm)
    llm.on("cadê o 1002?", order_status("1002"))
    conversation_id = await agent.new_conversation()

    with recording() as langsmith:
        await agent.say(conversation_id, "cadê o 1002?")

    assert langsmith.created == {}
