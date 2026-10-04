"""LLM provider interface and its implementations: Anthropic, and any OpenAI-compatible
endpoint such as the 9router (ADR-0004), chosen by configuration.

Tests use a scripted fake; only the evaluation suite talks to the real models.
"""

import enum
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from langsmith import traceable
from langsmith.wrappers import wrap_openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field, ValidationError

from support_agent.config import Settings


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
    order_ids: list[str | None] = Field(
        default_factory=list,
        description="For each intent, in the same order, the order number it is about as "
        "written, or null if none",
    )
    requested_amount_cents: int | None = Field(
        default=None, description="Refund amount the Customer asked for, in cents, if any"
    )
    asks_refund_status: bool = Field(
        default=False,
        description="True if the Customer asks how a refund already requested is going",
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
    unclear: bool = Field(
        default=False,
        description="True if you can't tell what the Customer wants from the message",
    )


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
- refund: wants money back for an order, or asks about the status of their own refund
- faq: questions about store policies (delivery times, exchanges, returns, the Refund Policy)
- other: greetings, small talk, anything else

A general question about how refunds work (timing, payment method, rules) is faq, not \
refund: the Customer is asking about the policy, not for their money back. When they name \
their own refund and ask when the money lands ("meu reembolso do ORD-1004 foi aprovado, \
quando cai?"), it is refund: they ask about the status of their own refund. When they ask \
for money back and also how refunds work ("quero reembolso do 1001, em quanto tempo cai?"), \
the intents are refund, then faq.

When the Customer asks for money back, the intent is refund, even if the message also says \
the order is late, lost or was returned to sender: that is the reason for the refund, not a \
separate order_status question. Use order_status only when they also ask where the order is \
or what happened to it.

Set unclear to true when you can't tell what the Customer wants: filler ("hmm", "sei lá"), \
or a vague reference to something never named in the conversation ("e aquilo lá?", \
"o negócio"). Then the intent is other and the agent will ask them to say it in other words. \
A greeting, small talk or an off-topic question is clear: other, with unclear false.

List every intent present, in the order raised. Extract the order number exactly as written \
(e.g. "123", "ORD-1002"), only when the last message names it. When intents are about \
different orders ("onde está o 1002? e quero reembolso do 1001"), order_ids gives each \
intent its own: ["1002", "1001"]. confidence is how sure you are about the first intent."""

CONFIRM_SYSTEM = """\
The support agent of a Brazilian online store asked the Customer to confirm an action \
(e.g. cancelling an order) and is waiting for an explicit answer. Classify the Customer's \
LAST message:
- confirm: an explicit, unconditional yes to exactly what the agent asked
- deny: an explicit no, or the Customer wants to keep things as they are
- other: anything else — doubts, questions, conditions, a different subject or order, or \
anything you are unsure about. When in doubt, answer other."""

# The traced run of the reply the LLM writes; the evaluation judges its text.
RESPONDER_RUN_NAME = "responder"

RESPOND_SYSTEM = """\
You are the customer support agent of a Brazilian online store, replying in Brazilian \
Portuguese to the Customer's last message. Be brief, warm and direct.

Use ONLY the facts provided. Never state a date, status, amount, carrier event or tracking \
code that is not in the facts, and never guess what will happen next. Write dates as \
DD/MM/AAAA, exactly as they appear in the facts. If the facts say the carrier could not be \
reached, say so plainly.

When the facts are about an Order, always give the delivery date if it was delivered; \
otherwise its tracking code (if it has one) and the promised delivery date, unless the carrier \
reports it lost or returned. Do so even if the Customer didn't ask.

When the facts are excerpts from the store's policies, answer only what they say and name the \
policy you rely on (e.g. "Segundo a Política de Trocas, ..."). If they don't answer the \
question, say you don't know instead of filling the gap."""


class AnthropicLLM:
    """Each call is an LLM span inside its node's span when LangSmith tracing is on."""

    def __init__(self, client: AsyncAnthropic, classifier_model: str, responder_model: str):
        self._client = client
        self._classifier_model = classifier_model
        self._responder_model = responder_model

    @traceable(run_type="llm")
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

    @traceable(run_type="llm")
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

    @traceable(run_type="llm", name=RESPONDER_RUN_NAME)
    async def respond(self, history: Sequence[ConversationMessage], facts: str) -> str:
        response = await self._client.messages.create(
            model=self._responder_model,
            max_tokens=1024,
            system=f"{RESPOND_SYSTEM}\n\n<facts>\n{facts}\n</facts>",
            messages=_to_params(history),
            output_config={"effort": "low"},
        )
        # Cut mid-sentence, a reply may still look grounded while saying nothing.
        if response.stop_reason == "max_tokens":
            return ""
        return "".join(block.text for block in response.content if block.type == "text")


def _to_params(history: Sequence[ConversationMessage]) -> list[MessageParam]:
    return [{"role": m.role, "content": m.content} for m in history]


class OpenAICompatibleLLM:
    """The same calls over chat completions, for endpoints without Anthropic's structured
    output: the schema goes in the prompt and the reply is parsed leniently, since a combo
    may route to models that wrap the JSON in prose or a code fence."""

    def __init__(self, client: AsyncOpenAI, classifier_model: str, responder_model: str):
        self._client = client
        self._classifier_model = classifier_model
        self._responder_model = responder_model

    @traceable(run_type="llm")
    async def classify(self, history: Sequence[ConversationMessage]) -> Classification:
        text = await complete(
            self._client,
            self._classifier_model,
            json_system(CLASSIFY_SYSTEM, Classification),
            history,
        )
        classification = parse_json_reply(text, Classification)
        if classification is None:
            # Unparseable: treat as an unconfident "other" so the agent asks again.
            return Classification(intents=[Intent.OTHER], confidence=0.0, entities=Entities())
        return classification

    @traceable(run_type="llm")
    async def classify_confirmation(self, history: Sequence[ConversationMessage]) -> Confirmation:
        text = await complete(
            self._client,
            self._classifier_model,
            json_system(CONFIRM_SYSTEM, ConfirmationReply),
            history,
        )
        reply = parse_json_reply(text, ConfirmationReply)
        # Unparseable is not a yes: nothing happens without an explicit confirm.
        return Confirmation.OTHER if reply is None else reply.decision

    @traceable(run_type="llm", name=RESPONDER_RUN_NAME)
    async def respond(self, history: Sequence[ConversationMessage], facts: str) -> str:
        system = f"{RESPOND_SYSTEM}\n\n<facts>\n{facts}\n</facts>"
        return await complete(self._client, self._responder_model, system, history)


# Room for the reasoning too: left unset, the routed provider applies its own default, and
# Cloudflare's 256 tokens on gpt-oss-120b cut most replies short (#36).
MAX_TOKENS = 2048


async def complete(
    client: AsyncOpenAI, model: str, system: str, history: Sequence[ConversationMessage]
) -> str:
    """The reply text of one chat completion, without any leading reasoning block.

    A reply cut by the token limit is no reply: it may stop mid-sentence and still look fine.
    """
    messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
    for m in history:
        if m.role == "user":
            messages.append({"role": "user", "content": m.content})
        else:
            messages.append({"role": "assistant", "content": m.content})
    response = await client.chat.completions.create(
        model=model, messages=messages, max_tokens=MAX_TOKENS
    )
    if not response.choices or response.choices[0].finish_reason == "length":
        return ""
    content = response.choices[0].message.content
    return _without_reasoning(content or "").strip()


def json_system(system: str, schema: type[BaseModel]) -> str:
    return (
        f"{system}\n\nAnswer with only a JSON object (no prose, no code fence) matching this "
        f"JSON Schema:\n{json.dumps(schema.model_json_schema())}"
    )


_REASONING_END = re.compile(r"</think(?:ing)?>")
_REASONING_START = re.compile(r"<think(?:ing)?>")


def _without_reasoning(text: str) -> str:
    """What follows the model's reasoning, if it shows any: a draft is never the answer.

    Some models omit the opening tag; reasoning cut off before its closing tag is no answer.
    """
    ends = list(_REASONING_END.finditer(text))
    if ends:
        return text[ends[-1].end() :]
    return "" if _REASONING_START.search(text) else text


def parse_json_reply[M: BaseModel](text: str, schema: type[M]) -> M | None:
    """The JSON object in the reply that fits the schema, or None if there is none, or
    several that disagree (a model weighing options hasn't answered)."""
    decoder = json.JSONDecoder()
    found: list[M] = []
    for start in (match.start() for match in re.finditer(r"\{", text)):
        try:
            value, _ = decoder.raw_decode(text, start)
            found.append(schema.model_validate(value))
        except (json.JSONDecodeError, ValidationError):
            continue
    if not found or any(other != found[0] for other in found[1:]):
        return None
    return found[0]


def build_llm(settings: Settings) -> LLM:
    """The LLM of the configured provider."""
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleLLM(
            openai_client(settings), settings.classifier_model, settings.responder_model
        )
    return AnthropicLLM(
        anthropic_client(settings),
        classifier_model=settings.classifier_model,
        responder_model=settings.responder_model,
    )


def anthropic_client(settings: Settings) -> AsyncAnthropic:
    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required")
    return AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())


def openai_client(settings: Settings) -> AsyncOpenAI:
    if not settings.llm_base_url:
        raise RuntimeError("LLM_BASE_URL is required with LLM_PROVIDER=openai_compatible")
    # A local router may not check keys, but the SDK wants a non-empty one.
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else "not-needed"
    client = AsyncOpenAI(
        base_url=settings.llm_base_url, api_key=key, timeout=settings.llm_timeout_seconds
    )
    # Traces each completion with the model that answered: with a combo, it varies per call.
    return wrap_openai(client)
