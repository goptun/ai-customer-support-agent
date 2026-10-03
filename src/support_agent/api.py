import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, Self

import httpx
from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from support_agent.approvals import ApprovalDecision
from support_agent.auth import InvalidToken, Principal, Role, issue_token, verify_token
from support_agent.cancellations import Cancellations
from support_agent.config import Settings, get_settings
from support_agent.conversations import ConversationNotFound, Conversations
from support_agent.db import session_factory
from support_agent.decisions import (
    AlreadyDecided,
    ApprovalRequestNotFound,
    ApprovalRequestView,
    Decisions,
)
from support_agent.graph import AgentDeps, RunContext, build_graph, run_turn
from support_agent.knowledge import Embedder, FastEmbedEmbedder, KnowledgeBase
from support_agent.llm import LLM, AnthropicLLM
from support_agent.logistics import LogisticsClient
from support_agent.models import ApprovalStatus, Attendant, Customer, MessageRole
from support_agent.orders import Orders
from support_agent.refunds import RefundPolicy, Refunds
from support_agent.tickets import Tickets
from support_agent.tools import RetryPolicy, ToolDeps


class DevTokenRequest(BaseModel):
    """Exactly one seeded identity: a Customer or an Attendant."""

    customer_id: str | None = None
    attendant_id: str | None = None

    @model_validator(mode="after")
    def _one_identity(self) -> Self:
        if (self.customer_id is None) == (self.attendant_id is None):
            raise ValueError("give exactly one of customer_id or attendant_id")
        return self


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    reply: str
    conversation_id: uuid.UUID
    intent: str
    pending_actions: list[dict[str, Any]]
    # Attendant decisions the Customer hadn't seen yet, shown before this reply.
    notices: list[str]


class MessageView(BaseModel):
    role: MessageRole
    content: str
    created_at: datetime


class DecisionBody(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason_on_reject(self) -> Self:
        self.reason = (self.reason or "").strip() or None
        if self.decision == "reject" and self.reason is None:
            raise ValueError("a reason is required to reject")
        return self


def _today() -> date:
    return datetime.now(UTC).date()


def _anthropic_llm(settings: Settings) -> LLM:
    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required")
    return AnthropicLLM(
        AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value()),
        classifier_model=settings.classifier_model,
        responder_model=settings.responder_model,
    )


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLM | None = None,
    embedder: Embedder | None = None,
    logistics_transport: httpx.AsyncBaseTransport | None = None,
    today: Callable[[], date] = _today,
) -> FastAPI:
    """Build the app. Tests inject a fake LLM and embedder, a logistics transport and a clock."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(settings.database_url)
        redis = Redis.from_url(settings.redis_url)
        async with (
            httpx.AsyncClient(
                base_url=settings.logistics_url, transport=logistics_transport
            ) as logistics_http,
            AsyncConnectionPool(
                settings.checkpointer_url,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=False,
            ) as pool,
        ):
            checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
            await checkpointer.setup()
            app.state.sessions = session_factory(engine)
            app.state.conversations = Conversations(
                app.state.sessions,
                redis,
                ttl_seconds=settings.working_state_ttl_seconds,
                window=settings.working_state_window,
            )
            app.state.decisions = Decisions(
                app.state.sessions, app.state.conversations, checkpointer
            )
            tools = ToolDeps(
                orders=Orders(app.state.sessions),
                logistics=LogisticsClient(
                    logistics_http, timeout_seconds=settings.logistics_timeout_seconds
                ),
                tickets=Tickets(app.state.sessions),
                cancellations=Cancellations(app.state.sessions),
                refunds=Refunds(
                    app.state.sessions,
                    RefundPolicy(
                        delivery_window_days=settings.refund_delivery_window_days,
                        late_after_days=settings.refund_late_after_days,
                    ),
                    app.state.decisions.workflow,
                ),
                knowledge_base=KnowledgeBase(
                    app.state.sessions,
                    embedder
                    if embedder is not None
                    else await asyncio.to_thread(FastEmbedEmbedder, settings.embedding_model),
                    top_k=settings.knowledge_top_k,
                    min_score=settings.knowledge_min_score,
                ),
                today=today,
            )
            app.state.graph = build_graph(
                checkpointer,
                AgentDeps(
                    llm=llm or _anthropic_llm(settings),
                    tools=tools,
                    history_window=settings.working_state_window,
                    retry=RetryPolicy(
                        retries=settings.tool_retries,
                        backoff_seconds=settings.tool_retry_backoff_seconds,
                    ),
                    failures_before_escalation=settings.tool_failures_before_escalation,
                    clarification_confidence=settings.clarification_confidence,
                    clarifications_before_escalation=settings.clarifications_before_escalation,
                    recent_order_days=settings.recent_order_days,
                    recent_orders_limit=settings.recent_orders_limit,
                ),
            )
            await app.state.decisions.resume_unsettled()
            yield
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Customer Support Agent", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def authenticated(role: Role) -> Callable[..., Principal]:
        def principal(
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ) -> Principal:
            unauthorized = HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            )
            if credentials is None:
                raise unauthorized
            try:
                found = verify_token(credentials.credentials, settings.jwt_secret)
            except InvalidToken:
                raise unauthorized from None
            if found.role is not role:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, f"{role.capitalize()} token required"
                )
            return found

        return principal

    current_customer = authenticated(Role.CUSTOMER)
    current_attendant = authenticated(Role.ATTENDANT)

    @app.post("/auth/dev-token")
    async def dev_token(body: DevTokenRequest, request: Request) -> TokenResponse:
        async with request.app.state.sessions() as session:
            if body.customer_id is not None:
                found = await session.get(Customer, body.customer_id)
                role = Role.CUSTOMER
            else:
                found = await session.get(Attendant, body.attendant_id)
                role = Role.ATTENDANT
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown seeded identity")
        principal = Principal(subject=found.id, role=role)
        return TokenResponse(
            access_token=issue_token(principal, settings.jwt_secret, settings.jwt_ttl_seconds)
        )

    @app.post("/chat")
    async def chat(
        body: ChatRequest,
        request: Request,
        customer: Annotated[Principal, Depends(current_customer)],
    ) -> ChatResponse:
        conversations: Conversations = request.app.state.conversations
        graph: CompiledStateGraph[Any, RunContext] = request.app.state.graph
        try:
            conversation_id = await conversations.open(customer.subject, body.conversation_id)
        except ConversationNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None

        decisions: Decisions = request.app.state.decisions
        notices = await decisions.surface_unseen(customer.subject, conversation_id)
        await conversations.record(conversation_id, MessageRole.CUSTOMER, body.message)
        turn = await run_turn(graph, conversation_id, customer.subject, body.message)
        await conversations.record(conversation_id, MessageRole.AGENT, turn.reply, turn.intent)

        return ChatResponse(
            reply=turn.reply,
            conversation_id=conversation_id,
            intent=turn.intent,
            pending_actions=turn.pending_actions,
            notices=notices,
        )

    @app.get("/conversations/{conversation_id}/messages")
    async def conversation_messages(
        conversation_id: uuid.UUID,
        request: Request,
        customer: Annotated[Principal, Depends(current_customer)],
    ) -> list[MessageView]:
        conversations: Conversations = request.app.state.conversations
        decisions: Decisions = request.app.state.decisions
        try:
            await conversations.open(customer.subject, conversation_id)  # checks ownership
        except ConversationNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None
        # Marked seen before reading: every decision marked is among the messages read.
        await decisions.mark_seen(conversation_id)
        return [
            MessageView(role=m.role, content=m.content, created_at=m.created_at)
            for m in await conversations.messages(conversation_id)
        ]

    @app.get("/approvals")
    async def list_approvals(
        request: Request,
        _: Annotated[Principal, Depends(current_attendant)],
        approval_status: Annotated[ApprovalStatus, Query(alias="status")] = (
            ApprovalStatus.PENDING
        ),
    ) -> list[ApprovalRequestView]:
        decisions: Decisions = request.app.state.decisions
        return await decisions.in_status(approval_status)

    @app.post("/approvals/{approval_request_id}")
    async def decide_approval_request(
        approval_request_id: uuid.UUID,
        body: DecisionBody,
        request: Request,
        attendant: Annotated[Principal, Depends(current_attendant)],
    ) -> ApprovalRequestView:
        decisions: Decisions = request.app.state.decisions
        try:
            return await decisions.decide(
                approval_request_id,
                ApprovalDecision(
                    approved=body.decision == "approve",
                    attendant_id=attendant.subject,
                    reason=body.reason,
                ),
            )
        except ApprovalRequestNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval Request not found") from None
        except AlreadyDecided:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Approval Request already decided"
            ) from None

    return app
