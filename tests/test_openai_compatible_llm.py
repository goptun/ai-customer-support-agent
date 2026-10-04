"""The OpenAI-compatible provider (e.g. the 9router), against a fake endpoint at the HTTP layer.

Combos route to models of varying discipline, so replies may wrap the JSON in a code fence or
prose, or open with a reasoning block; anything unparseable takes the same safe fallback as
with Anthropic.
"""

import json
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from support_agent.config import Settings
from support_agent.evaluation.judge import JudgedTurn, OpenAICompatibleJudge, Verdict
from support_agent.llm import (
    AnthropicLLM,
    Confirmation,
    ConversationMessage,
    Intent,
    OpenAICompatibleLLM,
    build_llm,
)

HISTORY = [ConversationMessage(role="user", content="Cadê o pedido ORD-1002?")]


class FakeEndpoint:
    """Answers every chat completion with the next scripted content; keeps the requests."""

    def __init__(self, *contents: str) -> None:
        self._contents: Iterator[str] = iter(contents)
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith("/chat/completions")
        self.requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "arsenal-vps",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": next(self._contents)},
                    }
                ],
            },
        )

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key="test",
            base_url="http://router.test/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle)),
        )


def llm(endpoint: FakeEndpoint) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        endpoint.client(), classifier_model="classifier", responder_model="responder"
    )


CLASSIFICATION = {
    "intents": ["order_status"],
    "confidence": 0.9,
    "entities": {"order_id": "ORD-1002"},
}


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(CLASSIFICATION),
        f"```json\n{json.dumps(CLASSIFICATION)}\n```",
        f"<think>the customer asks where the order is</think>\n{json.dumps(CLASSIFICATION)}",
        f"Claro! Aqui está:\n{json.dumps(CLASSIFICATION)}\nEspero ter ajudado.",
    ],
)
async def test_classifies_from_json_however_the_model_wraps_it(content: str) -> None:
    endpoint = FakeEndpoint(content)

    classification = await llm(endpoint).classify(HISTORY)

    assert classification.intents == [Intent.ORDER_STATUS]
    assert classification.entities.order_id == "ORD-1002"
    [request] = endpoint.requests
    assert request["model"] == "classifier"
    assert request["messages"][-1] == {"role": "user", "content": "Cadê o pedido ORD-1002?"}


async def test_an_unparseable_classification_is_an_unconfident_other() -> None:
    endpoint = FakeEndpoint('{"intents": ["teleport"], "confidence": 2}')

    classification = await llm(endpoint).classify(HISTORY)

    assert classification.intents == [Intent.OTHER]
    assert classification.confidence == 0.0


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"decision": "confirm"}', Confirmation.CONFIRM),
        ('{"decision": "deny"}', Confirmation.DENY),
        ("sim, pode cancelar", Confirmation.OTHER),  # not JSON: never a yes
        ('{"decision": "yes"}', Confirmation.OTHER),
    ],
)
async def test_only_a_parsed_confirm_confirms(content: str, expected: Confirmation) -> None:
    assert await llm(FakeEndpoint(content)).classify_confirmation(HISTORY) == expected


async def test_responds_with_the_reply_text_given_the_facts() -> None:
    endpoint = FakeEndpoint("<think>use the facts</think>Seu pedido chega em 03/09/2026.")

    reply = await llm(endpoint).respond(HISTORY, "Previsão de entrega: 03/09/2026.")

    assert reply == "Seu pedido chega em 03/09/2026."
    [request] = endpoint.requests
    assert request["model"] == "responder"
    assert "Previsão de entrega: 03/09/2026." in request["messages"][0]["content"]


TURN = JudgedTurn(message="Cadê o 1002?", reply="Chega amanhã.", tool_outputs=[])


async def test_the_judge_reads_its_verdict_from_json() -> None:
    endpoint = FakeEndpoint('{"unsupported_claims": ["Chega amanhã"], "hallucinated": true}')

    verdict = await OpenAICompatibleJudge(endpoint.client(), "judge").judge(TURN)

    assert verdict == Verdict(unsupported_claims=["Chega amanhã"], hallucinated=True)
    assert endpoint.requests[0]["model"] == "judge"


async def test_a_judge_without_a_verdict_fails_loudly() -> None:
    with pytest.raises(RuntimeError):
        await OpenAICompatibleJudge(FakeEndpoint("não sei").client(), "judge").judge(TURN)


def settings(**overrides: Any) -> Settings:
    # Not from .env: the developer's own provider choice must not leak into the test.
    return Settings(_env_file=None, database_url="", redis_url="", jwt_secret="x", **overrides)


def test_the_provider_is_chosen_by_configuration() -> None:
    assert isinstance(build_llm(settings(anthropic_api_key=SecretStr("k"))), AnthropicLLM)
    openai_compatible = build_llm(
        settings(llm_provider="openai_compatible", llm_base_url="http://router:20128/v1")
    )
    assert isinstance(openai_compatible, OpenAICompatibleLLM)


def test_each_provider_needs_its_endpoint_configured() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_llm(settings(anthropic_api_key=None))
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        build_llm(settings(llm_provider="openai_compatible", llm_base_url=None))
