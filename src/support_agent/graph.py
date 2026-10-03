"""The agent graph: classify_intent → resolve_context → act → respond, plus escalate.

Policy questions (`faq`) go classify_intent → consult_knowledge_base → respond instead.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter

from support_agent.llm import LLM, ConversationMessage, Intent
from support_agent.models import TicketPriority, TicketReason
from support_agent.orders import OrderSummary
from support_agent.replies import (
    ASK_ORDER_ID,
    CAPABILITIES,
    CLARIFICATION,
    KNOWLEDGE_NOT_FOUND,
    TICKET_FAILED,
    cite_sources,
    is_grounded,
    render_error,
    render_knowledge,
    render_order_status,
    render_queued,
    render_recent_orders,
    render_single_recent_order,
    render_ticket,
)
from support_agent.tickets import TicketResult
from support_agent.tools import (
    KnowledgeBaseResult,
    OrderStatusResult,
    RetryPolicy,
    ToolDeps,
    ToolError,
    call_with_retry,
    create_support_ticket,
    get_order_status,
    is_failure,
    search_knowledge_base,
)

ToolResult = OrderStatusResult | ToolError
_tool_result = TypeAdapter[ToolResult](ToolResult)
_order_summaries = TypeAdapter(list[OrderSummary])
_ticket_result = TypeAdapter[TicketResult | ToolError](TicketResult | ToolError)
_knowledge_result = TypeAdapter[KnowledgeBaseResult | ToolError](KnowledgeBaseResult | ToolError)

PRIORITY = {
    TicketReason.CUSTOMER_REQUESTED: TicketPriority.NORMAL,
    TicketReason.TOOL_FAILURE: TicketPriority.HIGH,
    TicketReason.CLARIFICATION_FAILED: TicketPriority.NORMAL,
}
SUMMARY_MESSAGES = 5
SUMMARY_MESSAGE_CHARS = 300  # keeps the summary within the tool's length limit


class QueuedIntent(TypedDict):
    intent: str
    order_id: str | None  # as the Customer wrote it alongside the Intent
    message: str  # the Customer message that raised it


class ConversationState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    intent: str
    # Per-turn fields, reset by classify_intent. Stored as JSON so checkpoints stay plain.
    order_id: str | None
    query: str  # the Customer message the handled Intent comes from
    tool_result: dict[str, Any] | None
    escalation: str | None  # TicketReason
    ticket: dict[str, Any] | None
    needs_clarification: bool  # not confident enough to act: ask instead
    recent_orders: list[dict[str, Any]] | None  # no Order given or focused: the ones to offer
    single_recent_order: bool  # order_id was taken from the Customer's only recent Order
    # Carried across turns.
    focused_order_id: str | None  # last Order whose ownership a tool verified
    tool_failures: dict[str, int]  # failed calls per tool in this Conversation
    clarifications: int  # consecutive Clarifications asked
    queued_intents: list[QueuedIntent]  # raised but not handled yet, oldest first


@dataclass(frozen=True)
class RunContext:
    """Per-run context from the authenticated request; never produced by the LLM."""

    customer_id: str
    conversation_id: uuid.UUID


@dataclass(frozen=True)
class AgentDeps:
    llm: LLM
    tools: ToolDeps
    history_window: int = 20
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    failures_before_escalation: int = 2
    clarification_confidence: float = 0.7
    clarifications_before_escalation: int = 2
    recent_order_days: int = 30
    recent_orders_limit: int = 5


@dataclass(frozen=True)
class Turn:
    reply: str
    intent: str
    pending_actions: list[dict[str, Any]] = field(default_factory=list)


def _history(state: ConversationState, window: int) -> list[ConversationMessage]:
    return [
        ConversationMessage(
            role="user" if isinstance(m, HumanMessage) else "assistant", content=m.text
        )
        for m in state["messages"][-window:]
    ]


def _ticket_summary(state: ConversationState, reason: TicketReason, tool: str | None) -> str:
    why = {
        TicketReason.CUSTOMER_REQUESTED: "o Customer pediu atendimento humano",
        TicketReason.TOOL_FAILURE: f"falhas repetidas na ferramenta {tool}",
        TicketReason.CLARIFICATION_FAILED: "o agente não entendeu o pedido após Clarifications",
    }[reason]
    said = [m.text for m in state["messages"] if isinstance(m, HumanMessage)]
    return "\n".join(
        [
            f"Motivo: {why}.",
            f"Intenção: {state['intent']}.",
            f"Pedido em foco: {state.get('focused_order_id') or 'nenhum'}.",
            "Últimas mensagens do Customer:",
            *(f"- {text[:SUMMARY_MESSAGE_CHARS]}" for text in said[-SUMMARY_MESSAGES:]),
        ]
    )


def build_graph(
    checkpointer: BaseCheckpointSaver[Any], deps: AgentDeps
) -> CompiledStateGraph[Any, RunContext]:
    async def classify_intent(state: ConversationState) -> ConversationState:
        classification = await deps.llm.classify(_history(state, deps.history_window))
        # Small talk next to a real request is not a request of its own.
        raised: list[Intent] = [i for i in classification.intents if i is not Intent.OTHER] or [
            Intent.OTHER
        ]
        order_id = classification.entities.order_id
        message = state["messages"][-1].text
        queued = list(state.get("queued_intents") or [])
        update: ConversationState = {
            "intent": raised[0].value,
            "order_id": order_id,
            "query": message,
            "tool_result": None,
            "ticket": None,
            "escalation": None,
            "needs_clarification": False,
            "recent_orders": None,
            "single_recent_order": False,
        }
        if classification.entities.wants_human:
            # Straight to a Ticket: no retention attempt. Understood, so no failed Clarification.
            update["escalation"] = TicketReason.CUSTOMER_REQUESTED.value
            update["clarifications"] = 0
            return update

        if classification.confidence < deps.clarification_confidence:
            asked = state.get("clarifications", 0)
            if asked >= deps.clarifications_before_escalation:
                update["escalation"] = TicketReason.CLARIFICATION_FAILED.value
                update["clarifications"] = 0
            else:
                update["needs_clarification"] = True
                update["clarifications"] = asked + 1
            return update

        update["clarifications"] = 0
        if raised == [Intent.OTHER] and queued:
            # Nothing new asked: carry on with what the Customer raised earlier.
            resumed = queued.pop(0)
            update["intent"], update["order_id"] = resumed["intent"], resumed["order_id"]
            update["query"] = resumed.get("message", message)  # absent in older checkpoints
        else:
            handled = raised[0].value
            queued = [item for item in queued if item["intent"] != handled]
            for intent in raised[1:]:
                if intent.value not in {handled, *(item["intent"] for item in queued)}:
                    queued.append(
                        QueuedIntent(intent=intent.value, order_id=order_id, message=message)
                    )
        update["queued_intents"] = queued
        return update

    async def resolve_context(
        state: ConversationState, runtime: Runtime[RunContext]
    ) -> ConversationState:
        # Explicit order id (ownership checked by the tool) > Focused Order > recent Orders.
        if state.get("order_id"):
            return {}
        if state.get("focused_order_id"):
            return {"order_id": state["focused_order_id"]}
        since = datetime.combine(
            deps.tools.today() - timedelta(days=deps.recent_order_days), time.min, UTC
        )
        recent = await deps.tools.orders.recent(
            runtime.context.customer_id, since, deps.recent_orders_limit
        )
        if len(recent) == 1:
            return {"order_id": recent[0].order_id, "single_recent_order": True}
        return {"recent_orders": [order.model_dump(mode="json") for order in recent]}

    async def act(state: ConversationState, runtime: Runtime[RunContext]) -> ConversationState:
        result = await call_with_retry(
            lambda: get_order_status(
                deps.tools, runtime.context.customer_id, {"order_id": state.get("order_id")}
            ),
            deps.retry,
        )
        update: ConversationState = {"tool_result": result.model_dump(mode="json")}
        if isinstance(result, OrderStatusResult):
            update["focused_order_id"] = result.order_id
        if is_failure(result):
            failures = dict(state.get("tool_failures") or {})
            failures["get_order_status"] = failures.get("get_order_status", 0) + 1
            update["tool_failures"] = failures
            if failures["get_order_status"] >= deps.failures_before_escalation:
                update["escalation"] = TicketReason.TOOL_FAILURE.value
        return update

    async def consult_knowledge_base(state: ConversationState) -> ConversationState:
        result = await search_knowledge_base(
            deps.tools.knowledge_base, {"query": state.get("query")}
        )
        return {"tool_result": result.model_dump(mode="json")}

    async def escalate(state: ConversationState, runtime: Runtime[RunContext]) -> ConversationState:
        escalation = state.get("escalation")
        assert escalation is not None  # routed here only when set
        reason = TicketReason(escalation)
        failing = "get_order_status" if reason is TicketReason.TOOL_FAILURE else None
        result = await create_support_ticket(
            deps.tools,
            runtime.context.customer_id,
            runtime.context.conversation_id,
            {
                "reason": reason,
                "priority": PRIORITY[reason],
                "summary": _ticket_summary(state, reason, failing),
                "order_id": state.get("focused_order_id"),
            },
        )
        return {"ticket": result.model_dump(mode="json")}

    async def respond(state: ConversationState) -> ConversationState:
        return {"messages": [AIMessage(content=await _reply(state))]}

    async def _reply(state: ConversationState) -> str:
        if state.get("ticket") is not None:
            ticket = _ticket_result.validate_python(state["ticket"])
            return TICKET_FAILED if isinstance(ticket, ToolError) else render_ticket(ticket)
        if state.get("needs_clarification"):
            return CLARIFICATION
        answer = await _answer(state)
        queued = state.get("queued_intents") or []
        if not queued:
            return answer
        return f"{answer}\n\n{render_queued([Intent(item['intent']) for item in queued])}"

    async def _answer(state: ConversationState) -> str:
        if state["intent"] == Intent.FAQ:
            return await _knowledge_reply(state)
        if state["intent"] != Intent.ORDER_STATUS:
            return CAPABILITIES
        if state.get("tool_result") is None:
            offered = state.get("recent_orders")
            if not offered:
                return ASK_ORDER_ID
            return render_recent_orders(_order_summaries.validate_python(offered))
        reply = await _order_status_reply(state)
        order_id = state.get("order_id")
        if state.get("single_recent_order") and order_id:
            return f"{render_single_recent_order(order_id)}\n{reply}"
        return reply

    async def _order_status_reply(state: ConversationState) -> str:
        result = _tool_result.validate_python(state["tool_result"])
        if isinstance(result, ToolError):
            return render_error(result)
        facts = render_order_status(result)
        reply = await deps.llm.respond(_history(state, deps.history_window), facts.text)
        return reply if reply.strip() and is_grounded(reply, facts) else facts.text

    async def _knowledge_reply(state: ConversationState) -> str:
        result = _knowledge_result.validate_python(state["tool_result"])
        if not isinstance(result, KnowledgeBaseResult) or not result.chunks:
            return KNOWLEDGE_NOT_FOUND
        facts = render_knowledge(result)
        # The question goes along: a resumed Intent's question isn't the last message.
        reply = await deps.llm.respond(
            _history(state, deps.history_window),
            f"Pergunta do Customer: {state['query']}\n\n{facts.text}",
        )
        grounded = reply if reply.strip() and is_grounded(reply, facts) else facts.text
        return cite_sources(grounded, result)

    def after_classify(
        state: ConversationState,
    ) -> Literal["escalate", "resolve_context", "consult_knowledge_base", "respond"]:
        if state.get("escalation"):
            return "escalate"
        if state.get("needs_clarification"):
            return "respond"
        if state["intent"] == Intent.FAQ:
            return "consult_knowledge_base"
        if state["intent"] != Intent.ORDER_STATUS:
            return "respond"
        return "resolve_context"

    def after_resolve(state: ConversationState) -> Literal["act", "respond"]:
        return "act" if state.get("order_id") else "respond"

    def after_act(state: ConversationState) -> Literal["escalate", "respond"]:
        return "escalate" if state.get("escalation") else "respond"

    builder = StateGraph(ConversationState, context_schema=RunContext)
    builder.add_node("classify_intent", classify_intent)
    builder.add_node("resolve_context", resolve_context)
    builder.add_node("act", act)
    builder.add_node("consult_knowledge_base", consult_knowledge_base)
    builder.add_node("escalate", escalate)
    builder.add_node("respond", respond)
    builder.add_edge(START, "classify_intent")
    builder.add_conditional_edges("classify_intent", after_classify)
    builder.add_conditional_edges("resolve_context", after_resolve)
    builder.add_conditional_edges("act", after_act)
    builder.add_edge("consult_knowledge_base", "respond")
    builder.add_edge("escalate", "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer)


async def run_turn(
    graph: CompiledStateGraph[Any, RunContext],
    conversation_id: uuid.UUID,
    customer_id: str,
    message: str,
) -> Turn:
    """Run one Customer message through the graph on the Conversation's thread."""
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": str(conversation_id)}},
        context=RunContext(customer_id=customer_id, conversation_id=conversation_id),
    )
    return Turn(reply=str(result["messages"][-1].content), intent=result["intent"])
