import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any

import httpx
from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from support_agent.auth import InvalidToken, Principal, Role, issue_token, verify_token
from support_agent.config import Settings, get_settings
from support_agent.conversations import ConversationNotFound, Conversations
from support_agent.db import session_factory
from support_agent.graph import AgentDeps, RunContext, build_graph, run_turn
from support_agent.llm import LLM, AnthropicLLM
from support_agent.logistics import LogisticsClient
from support_agent.models import Customer, MessageRole
from support_agent.orders import Orders
from support_agent.tools import ToolDeps


class DevTokenRequest(BaseModel):
    customer_id: str


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
    logistics_transport: httpx.AsyncBaseTransport | None = None,
    today: Callable[[], date] = _today,
) -> FastAPI:
    """Build the app. Tests inject a fake LLM, a logistics transport and a fixed clock."""
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
            tools = ToolDeps(
                orders=Orders(app.state.sessions),
                logistics=LogisticsClient(
                    logistics_http, timeout_seconds=settings.logistics_timeout_seconds
                ),
                today=today,
            )
            app.state.graph = build_graph(
                checkpointer,
                AgentDeps(
                    llm=llm or _anthropic_llm(settings),
                    tools=tools,
                    history_window=settings.working_state_window,
                ),
            )
            yield
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Customer Support Agent", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def current_customer(
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
            principal = verify_token(credentials.credentials, settings.jwt_secret)
        except InvalidToken:
            raise unauthorized from None
        if principal.role is not Role.CUSTOMER:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Customer token required")
        return principal

    @app.post("/auth/dev-token")
    async def dev_token(body: DevTokenRequest, request: Request) -> TokenResponse:
        async with request.app.state.sessions() as session:
            customer = await session.get(Customer, body.customer_id)
        if customer is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown seeded Customer")
        principal = Principal(subject=customer.id, role=Role.CUSTOMER)
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

        await conversations.record(conversation_id, MessageRole.CUSTOMER, body.message)
        turn = await run_turn(graph, str(conversation_id), customer.subject, body.message)
        await conversations.record(conversation_id, MessageRole.AGENT, turn.reply, turn.intent)

        return ChatResponse(
            reply=turn.reply,
            conversation_id=conversation_id,
            intent=turn.intent,
            pending_actions=turn.pending_actions,
        )

    return app
