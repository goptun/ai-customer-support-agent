import math
import re
import uuid
import zlib
from collections.abc import Sequence
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import traceable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.approvals import ApprovalWorkflow, build_approval_graph
from support_agent.knowledge import KnowledgeBase
from support_agent.llm import (
    RESPONDER_RUN_NAME,
    Classification,
    Confirmation,
    ConversationMessage,
    Entities,
    Intent,
)
from support_agent.models import EMBEDDING_DIM
from support_agent.refunds import RefundPolicy, Refunds


def order_status(order_id: str | None = None) -> Classification:
    return Classification(
        intents=[Intent.ORDER_STATUS], confidence=0.95, entities=Entities(order_id=order_id)
    )


def cancel_order(order_id: str | None = None) -> Classification:
    return Classification(
        intents=[Intent.CANCEL_ORDER], confidence=0.95, entities=Entities(order_id=order_id)
    )


def refund(
    order_id: str | None = None, amount_cents: int | None = None, *, status: bool = False
) -> Classification:
    return Classification(
        intents=[Intent.REFUND],
        confidence=0.95,
        entities=Entities(
            order_id=order_id, requested_amount_cents=amount_cents, asks_refund_status=status
        ),
    )


def status_then_refund(
    status_order: str | None,
    refund_order: str,
    *,
    order_id: str | None = None,
    amount_cents: int | None = None,
) -> Classification:
    """Two Intents, each about its own Order, as a real classifier tells them apart (#30)."""
    return Classification(
        intents=[Intent.ORDER_STATUS, Intent.REFUND],
        confidence=0.95,
        entities=Entities(
            order_id=order_id,
            order_ids=[status_order, refund_order],
            requested_amount_cents=amount_cents,
        ),
    )


def faq() -> Classification:
    return Classification(intents=[Intent.FAQ], confidence=0.95, entities=Entities())


class ScriptedLLM:
    """Fake LLM scripted by the Customer's message.

    Unscripted messages classify as `other` (also as answers to a confirmation); unscripted
    replies repeat the facts verbatim, like a perfectly grounded model would.
    """

    def __init__(self) -> None:
        self._classifications: dict[str, Classification] = {}
        self._confirmations: dict[str, Confirmation] = {}
        self._replies: dict[str, str] = {}

    def on(self, message: str, classification: Classification, reply: str | None = None) -> None:
        self._classifications[message] = classification
        if reply is not None:
            self._replies[message] = reply

    def on_confirmation(self, message: str, confirmation: Confirmation) -> None:
        self._confirmations[message] = confirmation

    async def classify_confirmation(self, history: Sequence[ConversationMessage]) -> Confirmation:
        return self._confirmations.get(_last_customer_message(history), Confirmation.OTHER)

    async def classify(self, history: Sequence[ConversationMessage]) -> Classification:
        return self._classifications.get(
            _last_customer_message(history),
            Classification(intents=[Intent.OTHER], confidence=1.0, entities=Entities()),
        )

    async def respond(self, history: Sequence[ConversationMessage], facts: str) -> str:
        return await _traced_reply(self._replies.get(_last_customer_message(history), facts))


@traceable(run_type="llm", name=RESPONDER_RUN_NAME)  # like the real LLMs
async def _traced_reply(reply: str) -> str:
    return reply


def _last_customer_message(history: Sequence[ConversationMessage]) -> str:
    return next(m.content for m in reversed(history) if m.role == "user")


async def never_settled(approval_request_id: uuid.UUID) -> None:
    """For approval workflows no test resumes; Decisions settles the ones that are."""
    raise AssertionError(f"unexpected settle of {approval_request_id}")


def refunds(
    sessions: async_sessionmaker[AsyncSession],
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> Refunds:
    """Refunds under the default Refund Policy, approval workflows checkpointed in memory."""
    workflow = ApprovalWorkflow(
        build_approval_graph(checkpointer or InMemorySaver(), never_settled)
    )
    return Refunds(sessions, RefundPolicy(), workflow)


class LexicalEmbedder:
    """Deterministic stand-in for the embedding model: hashed bag of word stems.

    Texts sharing words land close together, unrelated ones near zero, which is enough to
    test retrieval without downloading a model. Retrieval quality is measured in eval.
    """

    def __init__(self) -> None:
        self.passages_embedded = 0

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.passages_embedded += len(texts)
        return [_bag_of_stems(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return _bag_of_stems(text)


# The threshold for LexicalEmbedder scores; the real model's is a setting.
KNOWLEDGE_MIN_SCORE = 0.3


def lexical_knowledge_base(sessions: async_sessionmaker[AsyncSession]) -> KnowledgeBase:
    return KnowledgeBase(sessions, LexicalEmbedder(), top_k=3, min_score=KNOWLEDGE_MIN_SCORE)


def _bag_of_stems(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    for word in re.findall(r"\w{4,}", text.casefold()):
        vector[zlib.crc32(word[:5].encode()) % EMBEDDING_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]
