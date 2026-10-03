"""The agent graph: classify_intent → resolve_context → act → respond, plus escalate.

Policy questions (`faq`) go classify_intent → consult_knowledge_base → respond instead, and
questions about Refunds already requested go classify_intent → check_refunds → respond.
A Refund request never pauses this thread: its approval waits on a thread of its own
(ADR-0002), so the Customer keeps chatting meanwhile.
A Cancellation the Order allows is asked in respond, then the run pauses in
await_confirmation (an `interrupt()` on the Conversation thread) until the Customer's next
message: `confirm` → cancel, `deny` → keep it, anything else drops it and goes back to
classify_intent as a new message.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import TypeAdapter

from support_agent.cancellations import CancellationResult
from support_agent.llm import LLM, Confirmation, ConversationMessage, Intent
from support_agent.models import TicketPriority, TicketReason
from support_agent.orders import OrderSummary
from support_agent.refunds import RefundRequested
from support_agent.replies import (
    ASK_ORDER_ID,
    CAPABILITIES,
    CLARIFICATION,
    KNOWLEDGE_NOT_FOUND,
    TICKET_FAILED,
    cite_sources,
    is_grounded,
    render_cancellation_dropped,
    render_cancellation_kept,
    render_cancellation_question,
    render_cancelled,
    render_error,
    render_knowledge,
    render_order_status,
    render_queued,
    render_recent_orders,
    render_refund_changed,
    render_refund_refused,
    render_refund_requested,
    render_refund_status,
    render_single_recent_order,
    render_ticket,
)
from support_agent.tickets import TicketResult
from support_agent.tools import (
    REFUND_REASON_CHARS,
    CancellationPreview,
    ErrorCode,
    KnowledgeBaseResult,
    OrderStatusResult,
    RefundRefused,
    RefundStatusResult,
    RetryPolicy,
    ToolDeps,
    ToolError,
    call_with_retry,
    cancel_order,
    create_refund,
    create_support_ticket,
    get_order_status,
    get_refund_status,
    is_failure,
    preview_cancellation,
    search_knowledge_base,
)

ToolResult = OrderStatusResult | ToolError
_tool_result = TypeAdapter[ToolResult](ToolResult)
_order_summaries = TypeAdapter(list[OrderSummary])
_ticket_result = TypeAdapter[TicketResult | ToolError](TicketResult | ToolError)
_knowledge_result = TypeAdapter[KnowledgeBaseResult | ToolError](KnowledgeBaseResult | ToolError)
_CancellationToolResult = CancellationPreview | CancellationResult | ToolError
_cancellation_result = TypeAdapter[_CancellationToolResult](_CancellationToolResult)
_cancellation_preview = TypeAdapter(CancellationPreview)
_RefundToolResult = RefundRequested | RefundRefused | RefundStatusResult | ToolError
_refund_result = TypeAdapter[_RefundToolResult](_RefundToolResult)
ORDER_INTENTS = frozenset({Intent.ORDER_STATUS, Intent.CANCEL_ORDER, Intent.REFUND})
# Refusals that still prove the Order exists and is the Customer's.
OWNED_ORDER_ERRORS = frozenset({ErrorCode.ALREADY_DISPATCHED, ErrorCode.ALREADY_CANCELLED})

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
    # Entities of that message; absent in older checkpoints.
    requested_amount_cents: NotRequired[int | None]
    asks_refund_status: NotRequired[bool]


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
    confirmation: str | None  # Confirmation: the Customer's answer to the Cancellation question
    requested_amount_cents: int | None  # the Refund amount the Customer asked for
    asks_refund_status: bool  # about Refunds already requested, not a new one
    # Carried across turns.
    focused_order_id: str | None  # last Order whose ownership a tool verified
    tool_failures: dict[str, int]  # failed calls per tool in this Conversation
    clarifications: int  # consecutive Clarifications asked
    queued_intents: list[QueuedIntent]  # raised but not handled yet, oldest first
    # Set while a Cancellation awaits explicit confirmation (CancellationPreview).
    pending_cancellation: dict[str, Any] | None
    cancellation_notice: str | None  # what happened to a pending Cancellation; leads the reply


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
        entities = classification.entities
        order_id = entities.order_id
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
            "confirmation": None,
            "requested_amount_cents": entities.requested_amount_cents,
            "asks_refund_status": entities.asks_refund_status,
        }
        if entities.wants_human:
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
            update["requested_amount_cents"] = resumed.get("requested_amount_cents")
            update["asks_refund_status"] = resumed.get("asks_refund_status", False)
        else:
            handled = raised[0].value
            queued = [item for item in queued if item["intent"] != handled]
            for intent in raised[1:]:
                if intent.value not in {handled, *(item["intent"] for item in queued)}:
                    queued.append(
                        QueuedIntent(
                            intent=intent.value,
                            order_id=order_id,
                            message=message,
                            requested_amount_cents=entities.requested_amount_cents,
                            asks_refund_status=entities.asks_refund_status,
                        )
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
        if state["intent"] == Intent.CANCEL_ORDER:
            return await _check_cancellation(state, runtime.context.customer_id)
        if state["intent"] == Intent.REFUND:
            return await _request_refund(state, runtime.context)
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

    async def _check_cancellation(state: ConversationState, customer_id: str) -> ConversationState:
        preview = await preview_cancellation(
            deps.tools, customer_id, {"order_id": state.get("order_id")}
        )
        update: ConversationState = {"tool_result": preview.model_dump(mode="json")}
        if isinstance(preview, CancellationPreview):
            update["focused_order_id"] = preview.order_id
            update["pending_cancellation"] = preview.model_dump(mode="json")
        elif preview.code in OWNED_ORDER_ERRORS:
            update["focused_order_id"] = preview.order_id
        return update

    async def _request_refund(state: ConversationState, context: RunContext) -> ConversationState:
        # Order and amount as the Customer gave them; the ceiling is the tool's to compute.
        result = await call_with_retry(
            lambda: create_refund(
                deps.tools,
                context.customer_id,
                context.conversation_id,
                {
                    "order_id": state.get("order_id"),
                    "amount_cents": state.get("requested_amount_cents"),
                    "reason": state["query"][:REFUND_REASON_CHARS],
                },
            ),
            deps.retry,
        )
        update: ConversationState = {"tool_result": result.model_dump(mode="json")}
        if isinstance(result, RefundRequested | RefundRefused):
            update["focused_order_id"] = result.order_id
        return update

    async def check_refunds(
        state: ConversationState, runtime: Runtime[RunContext]
    ) -> ConversationState:
        # Only an Order the Customer named narrows it down: the Focused Order may be another.
        result = await get_refund_status(
            deps.tools, runtime.context.customer_id, {"order_id": state.get("order_id")}
        )
        update: ConversationState = {"tool_result": result.model_dump(mode="json")}
        if isinstance(result, RefundStatusResult) and result.order_id is not None:
            update["focused_order_id"] = result.order_id
        return update

    async def await_confirmation(state: ConversationState) -> ConversationState:
        pending = state.get("pending_cancellation")
        assert pending is not None  # routed here only when set
        preview = _cancellation_preview.validate_python(pending)
        # Pauses the run; the Customer's next message resumes it as the answer.
        answer = interrupt(
            {
                "type": "confirm_cancellation",
                "order_id": preview.order_id,
                "refund_cents": preview.refund_cents,
            }
        )
        message = HumanMessage(content=str(answer))
        decision = await deps.llm.classify_confirmation(
            _history({"messages": [*state["messages"], message]}, deps.history_window)
        )
        update: ConversationState = {
            "messages": [message],
            "order_id": preview.order_id,
            "pending_cancellation": None,
            "confirmation": decision.value,
            "single_recent_order": False,  # announced along with the question already
            # What the Customer confirmed: the cancel node acts on exactly this.
            "tool_result": preview.model_dump(mode="json"),
        }
        if decision is Confirmation.OTHER:
            update["cancellation_notice"] = render_cancellation_dropped(preview.order_id)
        return update

    async def cancel(state: ConversationState, runtime: Runtime[RunContext]) -> ConversationState:
        # Order and Refund are the ones the Customer confirmed, never ones the LLM picked.
        confirmed = _cancellation_preview.validate_python(state["tool_result"])
        result = await cancel_order(
            deps.tools,
            runtime.context.customer_id,
            {"order_id": confirmed.order_id, "expected_refund_cents": confirmed.refund_cents},
        )
        if isinstance(result, ToolError) and result.code is ErrorCode.REFUND_CHANGED:
            # Ask again with what cancelling would do now.
            update = await _check_cancellation(state, runtime.context.customer_id)
            update["cancellation_notice"] = render_refund_changed(confirmed.order_id)
            return update
        return {"tool_result": result.model_dump(mode="json")}

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
        reply = await _reply(state)
        notice = state.get("cancellation_notice")
        if notice:
            reply = f"{notice}\n\n{reply}"
        return {"messages": [AIMessage(content=reply)], "cancellation_notice": None}

    async def _reply(state: ConversationState) -> str:
        if state.get("ticket") is not None:
            ticket = _ticket_result.validate_python(state["ticket"])
            return TICKET_FAILED if isinstance(ticket, ToolError) else render_ticket(ticket)
        if state.get("needs_clarification"):
            return CLARIFICATION
        answer = await _answer(state)
        queued = state.get("queued_intents") or []
        # A yes/no question stands alone; queued Intents are announced after the answer.
        if not queued or state.get("pending_cancellation"):
            return answer
        return f"{answer}\n\n{render_queued([Intent(item['intent']) for item in queued])}"

    async def _answer(state: ConversationState) -> str:
        if state["intent"] == Intent.FAQ:
            return await _knowledge_reply(state)
        if state["intent"] not in ORDER_INTENTS:
            return CAPABILITIES
        if state.get("confirmation") == Confirmation.DENY:
            return render_cancellation_kept(state["order_id"] or "")
        if state.get("tool_result") is None:
            offered = state.get("recent_orders")
            if not offered:
                return ASK_ORDER_ID
            return render_recent_orders(_order_summaries.validate_python(offered))
        if state["intent"] == Intent.CANCEL_ORDER:
            reply = _cancellation_reply(state)
        elif state["intent"] == Intent.REFUND:
            reply = _refund_reply(state)
        else:
            reply = await _order_status_reply(state)
        order_id = state.get("order_id")
        if state.get("single_recent_order") and order_id:
            return f"{render_single_recent_order(order_id)}\n{reply}"
        return reply

    def _cancellation_reply(state: ConversationState) -> str:
        # Deterministic wording: what money moves is never left to the LLM.
        result = _cancellation_result.validate_python(state["tool_result"])
        if isinstance(result, ToolError):
            return render_error(result)
        if isinstance(result, CancellationPreview):
            return render_cancellation_question(result)
        return render_cancelled(result)

    def _refund_reply(state: ConversationState) -> str:
        # Deterministic wording, like Cancellations: eligibility and amounts come from code.
        result = _refund_result.validate_python(state["tool_result"])
        if isinstance(result, ToolError):
            return render_error(result)
        if isinstance(result, RefundRefused):
            return render_refund_refused(result)
        if isinstance(result, RefundStatusResult):
            return render_refund_status(result)
        return render_refund_requested(result)

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
    ) -> Literal[
        "escalate", "resolve_context", "consult_knowledge_base", "check_refunds", "respond"
    ]:
        if state.get("escalation"):
            return "escalate"
        if state.get("needs_clarification"):
            return "respond"
        if state["intent"] == Intent.FAQ:
            return "consult_knowledge_base"
        if state["intent"] == Intent.REFUND and state.get("asks_refund_status"):
            return "check_refunds"
        if state["intent"] not in ORDER_INTENTS:
            return "respond"
        return "resolve_context"

    def after_resolve(state: ConversationState) -> Literal["act", "respond"]:
        return "act" if state.get("order_id") else "respond"

    def after_act(state: ConversationState) -> Literal["escalate", "respond"]:
        return "escalate" if state.get("escalation") else "respond"

    def after_confirmation(
        state: ConversationState,
    ) -> Literal["cancel", "respond", "classify_intent"]:
        confirmation = state.get("confirmation")
        if confirmation == Confirmation.CONFIRM:
            return "cancel"
        if confirmation == Confirmation.DENY:
            return "respond"
        return "classify_intent"

    def after_respond(state: ConversationState) -> Literal["await_confirmation", "__end__"]:
        return "await_confirmation" if state.get("pending_cancellation") else "__end__"

    builder = StateGraph(ConversationState, context_schema=RunContext)
    builder.add_node("classify_intent", classify_intent)
    builder.add_node("resolve_context", resolve_context)
    builder.add_node("act", act)
    builder.add_node("consult_knowledge_base", consult_knowledge_base)
    builder.add_node("check_refunds", check_refunds)
    builder.add_node("await_confirmation", await_confirmation)
    builder.add_node("cancel", cancel)
    builder.add_node("escalate", escalate)
    builder.add_node("respond", respond)
    builder.add_edge(START, "classify_intent")
    builder.add_conditional_edges("classify_intent", after_classify)
    builder.add_conditional_edges("resolve_context", after_resolve)
    builder.add_conditional_edges("act", after_act)
    builder.add_edge("consult_knowledge_base", "respond")
    builder.add_edge("check_refunds", "respond")
    builder.add_conditional_edges("await_confirmation", after_confirmation)
    builder.add_edge("cancel", "respond")
    builder.add_edge("escalate", "respond")
    builder.add_conditional_edges("respond", after_respond)
    return builder.compile(checkpointer=checkpointer)


async def run_turn(
    graph: CompiledStateGraph[Any, RunContext],
    conversation_id: uuid.UUID,
    customer_id: str,
    message: str,
) -> Turn:
    """Run one Customer message through the graph on the Conversation's thread.

    A run paused awaiting confirmation is resumed with the message as the Customer's answer.
    With LANGSMITH_TRACING set, the turn is one LangSmith trace with a span per node.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": str(conversation_id)},
        "run_name": "conversation_turn",
        "metadata": {"conversation_id": str(conversation_id), "customer_id": customer_id},
    }
    paused = (await graph.aget_state(config)).interrupts
    result = await graph.ainvoke(
        Command(resume=message) if paused else {"messages": [HumanMessage(content=message)]},
        config=config,
        context=RunContext(customer_id=customer_id, conversation_id=conversation_id),
    )
    return Turn(
        reply=str(result["messages"][-1].content),
        intent=result["intent"],
        pending_actions=[pending.value for pending in result.get("__interrupt__", [])],
    )
