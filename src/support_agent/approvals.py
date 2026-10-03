"""The approval workflow: one graph thread per Approval Request, paused until an Attendant decides.

It runs apart from the Conversation thread (ADR-0002), so the Customer keeps chatting while
it waits, and is checkpointed in Postgres so the wait survives restarts (ADR-0001). Resumed
with the Attendant's decision, it settles it: issues the Refund if approved and relays the
decision to the Conversation.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from support_agent.models import ApprovalStatus, RefundStatus


class ApprovalPause(BaseModel):
    """What the paused workflow waits on; the interrupt's value."""

    type: Literal["approve_refund"] = "approve_refund"
    approval_request_id: uuid.UUID
    refund_id: int
    order_id: str
    amount_cents: int


class ApprovalDecision(BaseModel):
    """The Attendant's decision, already recorded; the resume value."""

    approved: bool
    attendant_id: str
    reason: str | None

    @property
    def status(self) -> ApprovalStatus:
        return ApprovalStatus.APPROVED if self.approved else ApprovalStatus.REJECTED

    @property
    def refund_status(self) -> RefundStatus:
        return RefundStatus.APPROVED if self.approved else RefundStatus.REJECTED


class ApprovalState(TypedDict, total=False):
    request: dict[str, Any]  # ApprovalPause
    decision: dict[str, Any] | None  # ApprovalDecision, given on resume


# Carries out a recorded decision; idempotent, as a crash can make it run again.
Settle = Callable[[uuid.UUID], Awaitable[None]]


def approval_thread_id(approval_request_id: uuid.UUID) -> str:
    return f"approval:{approval_request_id}"


def _config(approval_request_id: uuid.UUID) -> RunnableConfig:
    return {"configurable": {"thread_id": approval_thread_id(approval_request_id)}}


def build_approval_graph(
    checkpointer: BaseCheckpointSaver[Any], settle: Settle
) -> CompiledStateGraph[Any, None]:
    def await_decision(state: ApprovalState) -> ApprovalState:
        # Pauses the thread; resuming it with the Attendant's decision continues from here.
        return {"decision": interrupt(state["request"])}

    async def settle_decision(state: ApprovalState) -> ApprovalState:
        await settle(ApprovalPause.model_validate(state["request"]).approval_request_id)
        return {}

    builder = StateGraph(ApprovalState)
    builder.add_node("await_decision", await_decision)
    builder.add_node("settle", settle_decision)
    builder.add_edge(START, "await_decision")
    builder.add_edge("await_decision", "settle")
    return builder.compile(checkpointer=checkpointer)


class ApprovalWorkflow:
    def __init__(self, graph: CompiledStateGraph[Any, None]) -> None:
        self._graph = graph

    async def start(self, pause: ApprovalPause) -> None:
        """Run the workflow up to its pause; one already paused is left as is.

        One that a crash stopped before its pause runs on from its last checkpoint.
        """
        config = _config(pause.approval_request_id)
        state = await self._graph.aget_state(config)
        if state.interrupts:
            return
        if state.next:
            await self._graph.ainvoke(None, config=config)
            return
        if not state.values:
            await self._graph.ainvoke({"request": pause.model_dump(mode="json")}, config=config)

    async def pending(self, approval_request_id: uuid.UUID) -> ApprovalPause | None:
        """What the workflow is paused on, or None if it isn't paused."""
        interrupts = (await self._graph.aget_state(_config(approval_request_id))).interrupts
        return ApprovalPause.model_validate(interrupts[0].value) if interrupts else None

    async def resume(self, approval_request_id: uuid.UUID, decision: ApprovalDecision) -> None:
        """Continue the paused workflow with the decision; one already past it is left as is.

        One that a crash stopped while settling runs on from its last checkpoint.
        """
        config = _config(approval_request_id)
        state = await self._graph.aget_state(config)
        if state.interrupts:
            await self._graph.ainvoke(
                Command(resume=decision.model_dump(mode="json")), config=config
            )
        elif state.next:
            await self._graph.ainvoke(None, config=config)
