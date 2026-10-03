"""The approval workflow: one graph thread per Approval Request, paused until an Attendant decides.

It runs apart from the Conversation thread (ADR-0002), so the Customer keeps chatting while
it waits, and is checkpointed in Postgres so the wait survives restarts (ADR-0001).
"""

import uuid
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel


class ApprovalPause(BaseModel):
    """What the paused workflow waits on; the interrupt's value."""

    type: str = "approve_refund"
    approval_request_id: uuid.UUID
    refund_id: int
    order_id: str
    amount_cents: int


class ApprovalState(TypedDict, total=False):
    request: dict[str, Any]  # ApprovalPause
    decision: dict[str, Any] | None  # the Attendant's, given on resume


def approval_thread_id(approval_request_id: uuid.UUID) -> str:
    return f"approval:{approval_request_id}"


def build_approval_graph(
    checkpointer: BaseCheckpointSaver[Any],
) -> CompiledStateGraph[Any, None]:
    def await_decision(state: ApprovalState) -> ApprovalState:
        # Pauses the thread; resuming it with the Attendant's decision continues from here.
        return {"decision": interrupt(state["request"])}

    builder = StateGraph(ApprovalState)
    builder.add_node("await_decision", await_decision)
    builder.add_edge(START, "await_decision")
    return builder.compile(checkpointer=checkpointer)


class ApprovalWorkflow:
    def __init__(self, graph: CompiledStateGraph[Any, None]) -> None:
        self._graph = graph

    async def start(self, pause: ApprovalPause) -> None:
        """Run the workflow up to its pause; a workflow already started is left as is."""
        config: RunnableConfig = {
            "configurable": {"thread_id": approval_thread_id(pause.approval_request_id)}
        }
        if (await self._graph.aget_state(config)).values:
            return
        await self._graph.ainvoke({"request": pause.model_dump(mode="json")}, config=config)

    async def pending(self, approval_request_id: uuid.UUID) -> ApprovalPause | None:
        """What the workflow is paused on, or None if it isn't paused."""
        config: RunnableConfig = {
            "configurable": {"thread_id": approval_thread_id(approval_request_id)}
        }
        interrupts = (await self._graph.aget_state(config)).interrupts
        return ApprovalPause.model_validate(interrupts[0].value) if interrupts else None
