import asyncio
import enum
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from support_agent.approvals import ApprovalDecision
from support_agent.auth import (
    AttendantScope,
    ExpiredToken,
    InvalidToken,
    Principal,
    Role,
    issue_token,
    verify_token,
)
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
from support_agent.demo import CloudflareTurnstile, DemoVisitors, TurnstileVerifier
from support_agent.graph import AgentDeps, RunContext, build_graph, run_turn
from support_agent.knowledge import Embedder, FastEmbedEmbedder, KnowledgeBase
from support_agent.limits import (
    ConversationRateLimit,
    Idempotency,
    IdempotencyKeyInFlight,
    IdempotencyKeyReused,
    LLMBudget,
    MeteredLLM,
    RateLimited,
)
from support_agent.llm import LLM, build_llm
from support_agent.logistics import LogisticsClient
from support_agent.models import ApprovalStatus, Attendant, Customer, MessageRole
from support_agent.orders import Orders, OrderView
from support_agent.refunds import RefundPolicy, Refunds
from support_agent.tickets import Tickets, TicketView
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


class DemoVisitorRequest(BaseModel):
    turnstile_token: str = Field(min_length=1, max_length=2048)


class DemoVisitorResponse(TokenResponse):
    expires_in: int
    # Known up front, so the page plays the replay instead of a chat that would be refused.
    llm_cap_exhausted: bool
    customer_name: str
    orders: list[OrderView]


class DemoConfig(BaseModel):
    turnstile_site_key: str | None
    # Lets the page go straight to the replay, before the Turnstile check or a resumed chat.
    llm_cap_exhausted: bool


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


RATE_LIMITED_REPLY = (
    "Você enviou muitas mensagens em pouco tempo nesta conversa. Aguarde um pouco e tente de novo."
)
LLM_CAP_REPLY = (
    "O atendimento automático atingiu o limite diário de uso desta demonstração. "
    "Tente de novo amanhã."
)


class Signal(enum.StrEnum):
    """Machine-readable `code` next to `detail`, for the errors the demo page acts on."""

    # The Demo Visitor's token expired or its Customer is gone (Demo Reset): admit a new one.
    DEMO_RESTART = "demo_restart"
    LLM_CAP_EXHAUSTED = "llm_cap_exhausted"
    CONVERSATION_RATE_LIMITED = "conversation_rate_limited"


class SignalledHTTPException(HTTPException):
    def __init__(
        self,
        status_code: int,
        detail: str,
        signal: Signal,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code, detail, headers)
        self.signal = signal


def _too_many_requests(detail: str, signal: Signal, limited: RateLimited) -> HTTPException:
    return SignalledHTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail,
        signal,
        headers={"Retry-After": str(limited.retry_after_seconds)},
    )


STATIC = Path(__file__).parent / "static"


def _today() -> date:
    return datetime.now(UTC).date()


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLM | None = None,
    embedder: Embedder | None = None,
    logistics_transport: httpx.AsyncBaseTransport | None = None,
    turnstile: TurnstileVerifier | None = None,
    today: Callable[[], date] = _today,
) -> FastAPI:
    """Build the app. Tests inject a fake LLM and embedder, a logistics transport, a Turnstile
    verifier and a clock."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(settings.database_url)
        redis = Redis.from_url(settings.redis_url)
        async with (
            httpx.AsyncClient(
                base_url=settings.logistics_url, transport=logistics_transport
            ) as logistics_http,
            httpx.AsyncClient(timeout=10) as turnstile_http,
            AsyncConnectionPool(
                settings.checkpointer_url,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=False,
            ) as pool,
        ):
            checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
            await checkpointer.setup()
            app.state.sessions = session_factory(engine)
            app.state.demo_visitors = DemoVisitors(app.state.sessions)
            app.state.orders = Orders(app.state.sessions)
            app.state.turnstile = turnstile or (
                None
                if settings.turnstile_secret_key is None
                else CloudflareTurnstile(turnstile_http, settings.turnstile_secret_key)
            )
            app.state.conversations = Conversations(
                app.state.sessions,
                redis,
                ttl_seconds=settings.working_state_ttl_seconds,
                window=settings.working_state_window,
            )
            app.state.rate_limit = ConversationRateLimit(
                redis,
                turns=settings.conversation_turns_per_window,
                window_seconds=settings.conversation_rate_window_seconds,
            )
            app.state.llm_budget = LLMBudget(redis, settings.llm_calls_per_day)
            app.state.idempotency = Idempotency(
                redis,
                settings.idempotency_ttl_seconds,
                settings.idempotency_in_flight_ttl_seconds,
            )
            app.state.tickets = Tickets(app.state.sessions)
            app.state.decisions = Decisions(
                app.state.sessions, app.state.conversations, checkpointer
            )
            tools = ToolDeps(
                orders=app.state.orders,
                logistics=LogisticsClient(
                    logistics_http, timeout_seconds=settings.logistics_timeout_seconds
                ),
                tickets=app.state.tickets,
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
                    llm=MeteredLLM(llm or build_llm(settings), app.state.llm_budget.count),
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
        # Send the LangSmith traces still queued; returns at once when tracing is off.
        await asyncio.to_thread(wait_for_all_tracers)

    app = FastAPI(title="Customer Support Agent", lifespan=lifespan, root_path=settings.root_path)
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(SignalledHTTPException)
    async def signalled(_: Request, exc: SignalledHTTPException) -> JSONResponse:
        return JSONResponse(
            {"detail": exc.detail, "code": exc.signal}, exc.status_code, headers=exc.headers
        )

    def authenticated(
        role: Role, *, or_demo_visitor: bool = False
    ) -> Callable[..., Awaitable[Principal]]:
        async def principal(
            request: Request,
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ) -> Principal:
            challenge = {"WWW-Authenticate": "Bearer"}
            unauthorized = HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Invalid or missing token", headers=challenge
            )
            restart = SignalledHTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Demo Visitor no longer admitted",
                Signal.DEMO_RESTART,
                headers=challenge,
            )
            if credentials is None:
                raise unauthorized
            try:
                found = verify_token(credentials.credentials, settings.jwt_secret)
            except ExpiredToken as expired:
                # Tokens are not renewed: an expired Demo Visitor is admitted anew.
                raise (restart if expired.principal.is_demo_visitor else unauthorized) from None
            except InvalidToken:
                raise unauthorized from None
            if found.role is not role and not (or_demo_visitor and found.is_demo_visitor):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, f"{role.capitalize()} token required"
                )
            demo_visitors: DemoVisitors = request.app.state.demo_visitors
            if found.is_demo_visitor and not await demo_visitors.exists(found.subject):
                raise restart  # a Demo Reset removed its Customer
            return found

        return principal

    current_customer = authenticated(Role.CUSTOMER)
    # A Demo Visitor acts as their paired Attendant, over their own Customer only (ADR-0005).
    current_attendant = authenticated(Role.ATTENDANT, or_demo_visitor=True)

    @app.post("/auth/dev-token", include_in_schema=settings.dev_token_enabled)
    async def dev_token(body: DevTokenRequest, request: Request) -> TokenResponse:
        if not settings.dev_token_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
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

    # Liveness for the Compose healthcheck.
    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/demo/config")
    async def demo_config(request: Request) -> DemoConfig:
        llm_budget: LLMBudget = request.app.state.llm_budget
        return DemoConfig(
            turnstile_site_key=settings.turnstile_site_key,
            llm_cap_exhausted=await llm_budget.exhausted(),
        )

    @app.post("/demo/visitors", status_code=status.HTTP_201_CREATED)
    async def admit_demo_visitor(body: DemoVisitorRequest, request: Request) -> DemoVisitorResponse:
        verifier: TurnstileVerifier | None = request.app.state.turnstile
        if verifier is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Demo is not configured")
        if not await verifier.verify(body.turnstile_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Turnstile verification failed")
        demo_visitors: DemoVisitors = request.app.state.demo_visitors
        visitor = await demo_visitors.admit(today())
        principal = Principal(
            subject=visitor.customer_id, role=Role.CUSTOMER, attendant_id=visitor.attendant_id
        )
        orders: Orders = request.app.state.orders
        llm_budget: LLMBudget = request.app.state.llm_budget
        return DemoVisitorResponse(
            access_token=issue_token(
                principal, settings.jwt_secret, settings.demo_token_ttl_seconds
            ),
            expires_in=settings.demo_token_ttl_seconds,
            customer_name=visitor.customer_name,
            orders=await orders.of_customer(visitor.customer_id),
            llm_cap_exhausted=await llm_budget.exhausted(),
        )

    @app.get("/orders")
    async def customer_orders(
        request: Request, customer: Annotated[Principal, Depends(current_customer)]
    ) -> list[OrderView]:
        orders: Orders = request.app.state.orders
        return await orders.of_customer(customer.subject)

    async def take_turn(body: ChatRequest, request: Request, customer: Principal) -> ChatResponse:
        conversations: Conversations = request.app.state.conversations
        graph: CompiledStateGraph[Any, RunContext] = request.app.state.graph
        # Before opening: a refused first message mustn't leave an empty Conversation.
        try:
            await request.app.state.llm_budget.check()
        except RateLimited as limited:
            raise _too_many_requests(LLM_CAP_REPLY, Signal.LLM_CAP_EXHAUSTED, limited) from None
        try:
            conversation_id = await conversations.open(customer.subject, body.conversation_id)
        except ConversationNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None
        try:
            await request.app.state.rate_limit.take(conversation_id)
        except RateLimited as limited:
            raise _too_many_requests(
                RATE_LIMITED_REPLY, Signal.CONVERSATION_RATE_LIMITED, limited
            ) from None

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

    @app.post("/chat")
    async def chat(
        body: ChatRequest,
        request: Request,
        customer: Annotated[Principal, Depends(current_customer)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> ChatResponse:
        if idempotency_key is None:
            return await take_turn(body, request, customer)

        # A retry with the same key (a timeout, a double submit) gets the original turn back.
        idempotency: Idempotency = request.app.state.idempotency
        fingerprint = body.model_dump(mode="json")
        try:
            replay = await idempotency.claim(customer.subject, idempotency_key, fingerprint)
        except IdempotencyKeyReused:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Idempotency-Key already used for a different request",
            ) from None
        except IdempotencyKeyInFlight:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A request with this Idempotency-Key is still running"
            ) from None
        if replay is not None:
            return ChatResponse.model_validate(replay)
        try:
            response = await take_turn(body, request, customer)
        except BaseException:
            await idempotency.release(customer.subject, idempotency_key)
            raise
        await idempotency.settle(
            customer.subject, idempotency_key, fingerprint, response.model_dump(mode="json")
        )
        return response

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
        attendant: Annotated[Principal, Depends(current_attendant)],
        approval_status: Annotated[ApprovalStatus, Query(alias="status")] = (
            ApprovalStatus.PENDING
        ),
    ) -> list[ApprovalRequestView]:
        decisions: Decisions = request.app.state.decisions
        return await decisions.in_status(approval_status, AttendantScope.of(attendant).customer_id)

    @app.post("/approvals/{approval_request_id}")
    async def decide_approval_request(
        approval_request_id: uuid.UUID,
        body: DecisionBody,
        request: Request,
        attendant: Annotated[Principal, Depends(current_attendant)],
    ) -> ApprovalRequestView:
        decisions: Decisions = request.app.state.decisions
        scope = AttendantScope.of(attendant)
        try:
            return await decisions.decide(
                approval_request_id,
                ApprovalDecision(
                    approved=body.decision == "approve",
                    attendant_id=scope.attendant_id,
                    reason=body.reason,
                ),
                scope.customer_id,
            )
        except ApprovalRequestNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval Request not found") from None
        except AlreadyDecided:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Approval Request already decided"
            ) from None

    @app.get("/tickets")
    async def list_tickets(
        request: Request, attendant: Annotated[Principal, Depends(current_attendant)]
    ) -> list[TicketView]:
        tickets: Tickets = request.app.state.tickets
        return await tickets.listed(AttendantScope.of(attendant).customer_id)

    # Last, so the API routes take precedence: the demo page and its assets.
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app
