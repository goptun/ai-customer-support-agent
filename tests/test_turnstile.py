"""The production Turnstile verifier asks Cloudflare's siteverify and fails closed."""

from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from support_agent.demo import SITEVERIFY_URL, CloudflareTurnstile


def verifier(handler: httpx.MockTransport) -> CloudflareTurnstile:
    return CloudflareTurnstile(httpx.AsyncClient(transport=handler), SecretStr("the-secret"))


async def test_token_is_checked_with_the_secret_against_siteverify() -> None:
    seen: list[httpx.Request] = []

    def siteverify(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True})

    assert await verifier(httpx.MockTransport(siteverify)).verify("widget-token")
    [request] = seen
    assert str(request.url) == SITEVERIFY_URL
    assert parse_qs(request.content.decode()) == {
        "secret": ["the-secret"],
        "response": ["widget-token"],
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]}),
        httpx.Response(500),
        httpx.Response(200, text="not json"),
    ],
)
async def test_anything_but_success_is_a_refusal(response: httpx.Response) -> None:
    assert not await verifier(httpx.MockTransport(lambda _: response)).verify("widget-token")


async def test_cloudflare_unreachable_is_a_refusal() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    assert not await verifier(httpx.MockTransport(down)).verify("widget-token")
