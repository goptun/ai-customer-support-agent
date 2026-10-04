"""Drives scripted Conversations through the app's /chat and observes each turn.

Tool calls, per-node timings and the responder's wording come from the turn's trace, recorded
in process: tracing is switched on for the turn with a client that keeps the runs instead of
sending them, so the harness needs no LangSmith account and never mixes its runs with the
app's traces.
"""

import time
import uuid
from collections.abc import Callable
from typing import Any

from httpx import AsyncClient
from langsmith import Client, trace, tracing_context
from langsmith.run_trees import RunTree
from pydantic import BaseModel, ValidationError

from support_agent.evaluation.dataset import AttendantDecision, ScriptedConversation
from support_agent.graph import TURN_RUN_NAME
from support_agent.llm import RESPONDER_RUN_NAME
from support_agent.replies import (
    Facts,
    Fallback,
    fallback_reason,
    render_knowledge,
    render_order_status,
)
from support_agent.seed import ATTENDANTS
from support_agent.tools import (
    KnowledgeBaseResult,
    OrderStatusResult,
    get_order_status,
    search_knowledge_base,
)


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any]
    output: dict[str, Any] | None  # None if the call raised


class NodeTiming(BaseModel):
    node: str
    ms: float


# The Attendant who decides Approval Requests in scripted Conversations.
ATTENDANT_ID = ATTENDANTS[0]["id"]


class ResponderFallback(BaseModel):
    """A responder LLM reply discarded for the facts it was given."""

    llm_reply: str
    reason: Fallback | None  # None if the facts can't be rebuilt from the turn's tool calls


class TurnRun(BaseModel):
    message: str
    reply: str
    notices: list[str] = []  # Attendant decisions shown before the reply
    intent: str
    latency_ms: float  # end to end: the /chat request
    tool_calls: list[ToolCall]  # in call order, retries included
    nodes: list[NodeTiming]  # in run order
    # The responder LLM's text, if the reply uses it; None if the reply is all templates.
    llm_wording: str | None = None
    fallback: ResponderFallback | None = None


class ConversationRun(BaseModel):
    id: str
    turns: list[TurnRun]


class _KeepRuns(Client):
    """A LangSmith client that sends nothing: the run tree stays in memory for the harness."""

    def __init__(self) -> None:
        super().__init__(
            api_key="unused", api_url="http://langsmith.invalid", auto_batch_tracing=False
        )

    def create_run(self, *args: Any, **kwargs: Any) -> None:
        pass

    def update_run(self, *args: Any, **kwargs: Any) -> None:
        pass


async def run_conversation(
    client: AsyncClient, conversation: ScriptedConversation
) -> ConversationRun:
    token = await client.post("/auth/dev-token", json={"customer_id": conversation.customer_id})
    token.raise_for_status()
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    conversation_id: uuid.UUID | None = None
    turns = []
    for turn in conversation.turns:
        body: dict[str, Any] = {"message": turn.message}
        if conversation_id is not None:
            body["conversation_id"] = str(conversation_id)
        if turn.attendant is not None:
            await _decide_pending(client, conversation.customer_id, turn.attendant)
        with (
            tracing_context(enabled=True, client=_KeepRuns(), project_name="evaluation"),
            trace("evaluation_turn") as root,
        ):
            started = time.perf_counter()
            response = await client.post("/chat", json=body, headers=headers)
            latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            raise RuntimeError(
                f"{conversation.id}: /chat answered {response.status_code}: {response.text}"
            )
        data = response.json()
        conversation_id = uuid.UUID(data["conversation_id"])
        turn_run = _find(root, TURN_RUN_NAME)
        tool_calls = [_tool_call(run) for run in _descendants(turn_run, "tool")]
        llm_reply = _responder_reply(turn_run)
        llm_wording = llm_reply if llm_reply and llm_reply in data["reply"] else None
        turns.append(
            TurnRun(
                message=turn.message,
                reply=data["reply"],
                notices=data["notices"],
                intent=data["intent"],
                latency_ms=latency_ms,
                tool_calls=tool_calls,
                nodes=[
                    NodeTiming(node=node.name, ms=_ms(node))
                    for node in turn_run.child_runs
                    if node.end_time is not None
                ],
                llm_wording=llm_wording,
                fallback=(
                    None
                    if llm_reply is None or llm_wording is not None
                    else ResponderFallback(
                        llm_reply=llm_reply, reason=_fallback_reason(llm_reply, tool_calls)
                    )
                ),
            )
        )
    return ConversationRun(id=conversation.id, turns=turns)


async def _decide_pending(
    client: AsyncClient, customer_id: str, decision: AttendantDecision
) -> None:
    token = await client.post("/auth/dev-token", json={"attendant_id": ATTENDANT_ID})
    token.raise_for_status()
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    pending = await client.get("/approvals", params={"status": "pending"}, headers=headers)
    pending.raise_for_status()
    requests = [r for r in pending.json() if r["customer_id"] == customer_id]
    if not requests:
        raise RuntimeError(f"{customer_id} has no pending Approval Request to decide")
    for request in requests:
        response = await client.post(
            f"/approvals/{request['approval_request_id']}",
            json=decision.model_dump(),
            headers=headers,
        )
        response.raise_for_status()


def _find(root: RunTree, name: str) -> RunTree:
    for run in [root, *_descendants(root)]:
        if run.name == name:
            return run
    raise RuntimeError(f"no {name} run in the turn's trace")


def _descendants(run: RunTree, run_type: str | None = None) -> list[RunTree]:
    found = []
    for child in run.child_runs:
        if run_type is None or child.run_type == run_type:
            found.append(child)
        found.extend(_descendants(child, run_type))
    return sorted(found, key=lambda descendant: descendant.start_time)


def _responder_reply(turn_run: RunTree) -> str | None:
    """What the responder LLM wrote this turn, or None if it didn't run."""
    responses = [run for run in _descendants(turn_run, "llm") if run.name == RESPONDER_RUN_NAME]
    if not responses:
        return None
    return str((responses[-1].outputs or {}).get("output") or "").strip()


def _fallback_reason(llm_reply: str, tool_calls: list[ToolCall]) -> Fallback | None:
    """Why the reply was discarded, checked against the facts the responder was given.

    The facts are rebuilt from the tool output they were rendered from, the way the responder
    nodes render them.
    """
    for call in reversed(tool_calls):
        facts: Facts | None = None
        if call.name == get_order_status.__name__:
            facts = _facts(call, OrderStatusResult, render_order_status)
        elif call.name == search_knowledge_base.__name__:
            facts = _facts(call, KnowledgeBaseResult, render_knowledge)
        if facts is not None:
            return fallback_reason(llm_reply, facts)
    return None


def _facts[R: BaseModel](
    call: ToolCall, result: type[R], render: Callable[[R], Facts]
) -> Facts | None:
    try:
        return render(result.model_validate(call.output))
    except ValidationError:  # a ToolError, or the call raised
        return None


def _tool_call(run: RunTree) -> ToolCall:
    return ToolCall(name=run.name, args=dict(run.inputs.get("args") or {}), output=run.outputs)


def _ms(run: RunTree) -> float:
    assert run.end_time is not None
    return (run.end_time - run.start_time).total_seconds() * 1000
