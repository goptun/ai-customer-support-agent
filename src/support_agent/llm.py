"""LLM provider interface and its Anthropic implementation.

Tests use a scripted fake; only the evaluation suite talks to the real models.
"""

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from pydantic import BaseModel, Field


class Intent(enum.StrEnum):
    ORDER_STATUS = "order_status"
    CANCEL_ORDER = "cancel_order"
    REFUND = "refund"
    FAQ = "faq"
    OTHER = "other"


class Confirmation(enum.StrEnum):
    """How the Customer answered a request for explicit confirmation."""

    CONFIRM = "confirm"
    DENY = "deny"
    OTHER = "other"  # anything that is not a clear yes or no


class ConfirmationReply(BaseModel):
    decision: Confirmation


class Entities(BaseModel):
    order_id: str | None = Field(
        default=None, description="Order number exactly as the Customer wrote it, if any"
    )
    requested_amount_cents: int | None = Field(
        default=None, description="Refund amount the Customer asked for, in cents, if any"
    )
    wants_human: bool = Field(
        default=False, description="True if the Customer asks to talk to a human"
    )


class Classification(BaseModel):
    intents: list[Intent] = Field(
        min_length=1, description="Intents in the order the Customer raised them"
    )
    confidence: float = Field(ge=0, le=1)
    entities: Entities


@dataclass(frozen=True)
class ConversationMessage:
    role: Literal["user", "assistant"]
    content: str


class LLM(Protocol):
    async def classify(self, history: Sequence[ConversationMessage]) -> Classification: ...

    async def classify_confirmation(self, history: Sequence[ConversationMessage]) -> Confirmation:
        """Classify the last Customer message as the answer to the agent's yes/no question."""
        ...

    async def respond(self, history: Sequence[ConversationMessage], facts: str) -> str:
        """Write the reply to the last Customer message, using only `facts`."""
        ...


CLASSIFY_SYSTEM = """\
You classify messages sent to the customer support agent of a Brazilian online store.
Classify the Customer's LAST message, using earlier messages only as context.

Intents:
- order_status: where an order is, when it arrives, tracking, delays
- cancel_order: wants to cancel an order
- refund: wants money back, or asks about a refund
- faq: questions about store policies (delivery times, exchanges, returns, refund rules)
- other: greetings, small talk, anything else

List every intent present, in the order raised. Extract the order number exactly as written \
(e.g. "123", "ORD-1002"). confidence is how sure you are about the first intent."""

CONFIRM_SYSTEM = """\
The support agent of a Brazilian online store asked the Customer to confirm an action \
(e.g. cancelling an order) and is waiting for an explicit answer. Classify the Customer's \
LAST message:
- confirm: an explicit, unconditional yes to exactly what the agent asked
- deny: an explicit no, or the Customer wants to keep things as they are
- other: anything else — doubts, questions, conditions, a different subject or order, or \
anything you are unsure about. When in doubt, answer other."""

RESPOND_SYSTEM = """\
You are the customer support agent of a Brazilian online store, replying in Brazilian \
Portuguese to the Customer's last message. Be brief, warm and direct.

Use ONLY the facts provided. Never state a date, status, amount, carrier event or tracking \
code that is not in the facts, and never guess what will happen next. Write dates as \
DD/MM/AAAA, exactly as they appear in the facts. If the facts say the carrier could not be \
reached, say so plainly.

When the facts are excerpts from the store's policies, answer only what they say and name the \
policy you rely on (e.g. "Segundo a Política de Trocas, ..."). If they don't answer the \
question, say you don't know instead of filling the gap."""


class AnthropicLLM:
    def __init__(self, client: AsyncAnthropic, classifier_model: str, responder_model: str):
        self._client = client
        self._classifier_model = classifier_model
        self._responder_model = responder_model

    async def classify(self, history: Sequence[ConversationMessage]) -> Classification:
        response = await self._client.messages.parse(
            model=self._classifier_model,
            max_tokens=512,
            system=CLASSIFY_SYSTEM,
            messages=_to_params(history),
            output_format=Classification,
        )
        if response.parsed_output is None:
            # Refusal or truncation: treat as an unconfident "other" so the agent asks again.
            return Classification(intents=[Intent.OTHER], confidence=0.0, entities=Entities())
        return response.parsed_output

    async def classify_confirmation(self, history: Sequence[ConversationMessage]) -> Confirmation:
        response = await self._client.messages.parse(
            model=self._classifier_model,
            max_tokens=128,
            system=CONFIRM_SYSTEM,
            messages=_to_params(history),
            output_format=ConfirmationReply,
        )
        # Refusal or truncation is not a yes: nothing happens without an explicit confirm.
        if response.parsed_output is None:
            return Confirmation.OTHER
        return response.parsed_output.decision

    async def respond(self, history: Sequence[ConversationMessage], facts: str) -> str:
        response = await self._client.messages.create(
            model=self._responder_model,
            max_tokens=1024,
            system=f"{RESPOND_SYSTEM}\n\n<facts>\n{facts}\n</facts>",
            messages=_to_params(history),
            output_config={"effort": "low"},
        )
        return "".join(block.text for block in response.content if block.type == "text")


def _to_params(history: Sequence[ConversationMessage]) -> list[MessageParam]:
    return [{"role": m.role, "content": m.content} for m in history]
