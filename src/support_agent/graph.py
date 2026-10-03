"""The agent graph: classify_intent → resolve_context → act → respond."""

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter

from support_agent.llm import LLM, ChatMessage, Intent
from support_agent.replies import (
    ASK_ORDER_ID,
    CAPABILITIES,
    is_grounded,
    render_error,
    render_order_status,
)
from support_agent.tools import OrderStatusResult, ToolDeps, ToolError, get_order_status

ToolResult = OrderStatusResult | ToolError
_tool_result = TypeAdapter[ToolResult](ToolResult)


class ConversationState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    intent: str
    # Per-turn fields, reset by classify_intent. Stored as JSON so checkpoints stay plain.
    order_id: str | None
    tool_result: dict[str, Any] | None


@dataclass(frozen=True)
class RunContext:
    """Per-run context from the authenticated request; never produced by the LLM."""

    customer_id: str


@dataclass(frozen=True)
class AgentDeps:
    llm: LLM
    tools: ToolDeps
    history_window: int = 20


@dataclass(frozen=True)
class Turn:
    reply: str
    intent: str
    pending_actions: list[dict[str, Any]] = field(default_factory=list)


def _history(state: ConversationState, window: int) -> list[ChatMessage]:
    return [
        ChatMessage(role="user" if isinstance(m, HumanMessage) else "assistant", content=m.text)
        for m in state["messages"][-window:]
    ]


def build_graph(
    checkpointer: BaseCheckpointSaver[Any], deps: AgentDeps
) -> CompiledStateGraph[Any, RunContext]:
    async def classify_intent(state: ConversationState) -> ConversationState:
        classification = await deps.llm.classify(_history(state, deps.history_window))
        return {
            "intent": classification.intents[0].value,
            "order_id": classification.entities.order_id,
            "tool_result": None,
        }

    async def resolve_context(state: ConversationState) -> ConversationState:
        # Only an explicit order id for now; Focused Order and listing come later.
        return {}

    async def act(state: ConversationState, runtime: Runtime[RunContext]) -> ConversationState:
        result = await get_order_status(
            deps.tools, runtime.context.customer_id, {"order_id": state.get("order_id")}
        )
        return {"tool_result": result.model_dump(mode="json")}

    async def respond(state: ConversationState) -> ConversationState:
        return {"messages": [AIMessage(content=await _reply(state))]}

    async def _reply(state: ConversationState) -> str:
        if state["intent"] != Intent.ORDER_STATUS:
            return CAPABILITIES
        if state.get("tool_result") is None:
            return ASK_ORDER_ID
        result = _tool_result.validate_python(state["tool_result"])
        if isinstance(result, ToolError):
            return render_error(result)
        facts = render_order_status(result)
        reply = await deps.llm.respond(_history(state, deps.history_window), facts)
        return reply if reply.strip() and is_grounded(reply, facts) else facts

    def after_classify(state: ConversationState) -> Literal["resolve_context", "respond"]:
        return "resolve_context" if state["intent"] == Intent.ORDER_STATUS else "respond"

    def after_resolve(state: ConversationState) -> Literal["act", "respond"]:
        return "act" if state.get("order_id") else "respond"

    builder = StateGraph(ConversationState, context_schema=RunContext)
    builder.add_node("classify_intent", classify_intent)
    builder.add_node("resolve_context", resolve_context)
    builder.add_node("act", act)
    builder.add_node("respond", respond)
    builder.add_edge(START, "classify_intent")
    builder.add_conditional_edges("classify_intent", after_classify)
    builder.add_conditional_edges("resolve_context", after_resolve)
    builder.add_edge("act", "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer)


async def run_turn(
    graph: CompiledStateGraph[Any, RunContext], thread_id: str, customer_id: str, message: str
) -> Turn:
    """Run one Customer message through the graph on the Conversation's thread."""
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": thread_id}},
        context=RunContext(customer_id=customer_id),
    )
    return Turn(reply=str(result["messages"][-1].content), intent=result["intent"])
