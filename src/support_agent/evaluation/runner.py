"""Drives scripted Conversations through the app's /chat and observes each turn.

Tool calls and per-node timings come from the turn's trace, recorded in process: tracing is
switched on for the turn with a client that keeps the runs instead of sending them, so the
harness needs no LangSmith account and never mixes its runs with the app's traces.
"""

import time
import uuid
from typing import Any

from httpx import AsyncClient
from langsmith import Client, trace, tracing_context
from langsmith.run_trees import RunTree
from pydantic import BaseModel

from support_agent.evaluation.dataset import AttendantDecision, ScriptedConversation
from support_agent.graph import TURN_RUN_NAME
from support_agent.seed import ATTENDANTS


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any]
    output: dict[str, Any] | None  # None if the call raised


class NodeTiming(BaseModel):
    node: str
    ms: float


# The Attendant who decides Approval Requests in scripted Conversations.
ATTENDANT_ID = ATTENDANTS[0]["id"]


class TurnRun(BaseModel):
    message: str
    reply: str
    notices: list[str] = []  # Attendant decisions shown before the reply
    intent: str
    latency_ms: float  # end to end: the /chat request
    tool_calls: list[ToolCall]  # in call order, retries included
    nodes: list[NodeTiming]  # in run order


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
        turns.append(
            TurnRun(
                message=turn.message,
                reply=data["reply"],
                notices=data["notices"],
                intent=data["intent"],
                latency_ms=latency_ms,
                tool_calls=[_tool_call(run) for run in _descendants(turn_run, "tool")],
                nodes=[
                    NodeTiming(node=node.name, ms=_ms(node))
                    for node in turn_run.child_runs
                    if node.end_time is not None
                ],
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


def _tool_call(run: RunTree) -> ToolCall:
    return ToolCall(name=run.name, args=dict(run.inputs.get("args") or {}), output=run.outputs)


def _ms(run: RunTree) -> float:
    assert run.end_time is not None
    return (run.end_time - run.start_time).total_seconds() * 1000
