"""Demo Visitors: admitted after a Turnstile check, each with their own Customer and Attendant."""

from typing import Any

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.auth import Principal, Role, issue_token
from support_agent.config import Settings
from support_agent.db import session_factory
from support_agent.models import Attendant, Customer
from support_agent.seed import ORDERS

from .conftest import TEST_JWT_SECRET, TEST_TURNSTILE_SITE_KEY, app_client
from .fakes import FakeTurnstile, ScriptedLLM, order_status, refund
from .test_api import ANA, chat
from .test_refund_tools import approval_requests_of


async def admitted(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/demo/visitors", json={"turnstile_token": FakeTurnstile.PASSING})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(visitor: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {visitor['access_token']}"}


def claims_of(visitor: dict[str, Any]) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        visitor["access_token"], TEST_JWT_SECRET, algorithms=["HS256"]
    )
    return claims


def order_in(visitor: dict[str, Any], status: str, *, refundable: bool = False) -> str:
    """The id of the visitor's Order in `status`; for delivered ones, the recent or old one."""
    matches = [o for o in visitor["orders"] if o["status"] == status]
    if status == "delivered":
        matches.sort(key=lambda o: o["delivered_at"], reverse=refundable)
    order_id: str = matches[0]["order_id"]
    return order_id


async def test_admission_creates_a_customer_with_the_templated_orders(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    visitor = await admitted(client)

    assert visitor["token_type"] == "bearer"
    assert visitor["expires_in"] == 7200
    assert visitor["llm_cap_exhausted"] is False
    claims = claims_of(visitor)
    assert claims["role"] == "customer"
    assert claims["exp"] - claims["iat"] == 7200
    statuses = sorted(o["status"] for o in visitor["orders"])
    assert statuses == ["delivered", "delivered", "processing", "shipped"]
    async with session_factory(engine)() as session:
        assert await session.get(Customer, claims["sub"]) is not None
        assert await session.get(Attendant, claims["attendant"]) is not None


async def test_admission_is_refused_without_a_passing_turnstile_token(
    client: AsyncClient,
) -> None:
    missing = await client.post("/demo/visitors", json={})
    failed = await client.post("/demo/visitors", json={"turnstile_token": "forged"})

    assert missing.status_code == 422
    assert failed.status_code == 403


async def test_each_visitor_gets_their_own_customer_orders_and_attendant(
    client: AsyncClient,
) -> None:
    first, second = await admitted(client), await admitted(client)

    assert claims_of(first)["sub"] != claims_of(second)["sub"]
    assert claims_of(first)["attendant"] != claims_of(second)["attendant"]
    first_orders = {o["order_id"] for o in first["orders"]}
    second_orders = {o["order_id"] for o in second["orders"]}
    assert first_orders.isdisjoint(second_orders)


async def test_visitor_chats_about_their_orders(client: AsyncClient, llm: ScriptedLLM) -> None:
    visitor = await admitted(client)
    order_id = order_in(visitor, "processing")
    llm.on(f"onde está o {order_id}?", order_status(order_id))

    turn = await chat(client, bearer(visitor), f"onde está o {order_id}?")

    assert order_id in str(turn["reply"])
    assert "não encontrei" not in str(turn["reply"]).casefold()


async def test_dispatched_order_is_tracked_by_the_carrier(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    visitor = await admitted(client)
    order_id = order_in(visitor, "shipped")
    llm.on(f"cadê o {order_id}?", order_status(order_id))

    turn = await chat(client, bearer(visitor), f"cadê o {order_id}?")

    assert "em trânsito" in str(turn["reply"]).casefold()


async def test_recently_delivered_order_is_eligible_for_a_refund_needing_approval(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    visitor = await admitted(client)
    order_id = order_in(visitor, "delivered", refundable=True)
    llm.on(f"quero reembolso do {order_id}", refund(order_id))

    await chat(client, bearer(visitor), f"quero reembolso do {order_id}")

    [approval] = await approval_requests_of(engine, order_id)
    assert approval.status == "pending"


async def test_visitor_cannot_reach_another_visitors_conversations_or_orders(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    owner, intruder = await admitted(client), await admitted(client)
    turn = await chat(client, bearer(owner), "oi")
    owners_order = order_in(owner, "processing")
    llm.on(f"status do {owners_order}", order_status(owners_order))

    read = await client.get(
        f"/conversations/{turn['conversation_id']}/messages", headers=bearer(intruder)
    )
    posted = await client.post(
        "/chat",
        json={"message": "oi", "conversation_id": turn["conversation_id"]},
        headers=bearer(intruder),
    )
    asked = await chat(client, bearer(intruder), f"status do {owners_order}")
    listed = await client.get("/orders", headers=bearer(intruder))

    assert read.status_code == 404
    assert posted.status_code == 404
    assert f"Não encontrei o pedido {owners_order}" in str(asked["reply"])
    assert {o["order_id"] for o in listed.json()} == {o["order_id"] for o in intruder["orders"]}


def restart_signal(response: Any) -> bool:
    return bool(response.status_code == 401 and response.json().get("code") == "demo_restart")


async def test_an_expired_visitor_token_gets_the_restart_signal(client: AsyncClient) -> None:
    claims = claims_of(await admitted(client))
    principal = Principal(claims["sub"], Role.CUSTOMER, attendant_id=claims["attendant"])
    expired = {"Authorization": f"Bearer {issue_token(principal, TEST_JWT_SECRET, -60)}"}

    orders = await client.get("/orders", headers=expired)
    posted = await client.post("/chat", json={"message": "oi"}, headers=expired)

    assert restart_signal(orders)
    assert restart_signal(posted)


async def test_a_token_whose_customer_is_gone_gets_the_restart_signal(
    client: AsyncClient,
) -> None:
    # As after a Demo Reset: the token is still valid, its Customer no longer exists.
    gone = Principal("demo_gone", Role.CUSTOMER, attendant_id="demo_att_gone")
    headers = {"Authorization": f"Bearer {issue_token(gone, TEST_JWT_SECRET, 600)}"}

    orders = await client.get("/orders", headers=headers)
    posted = await client.post("/chat", json={"message": "oi"}, headers=headers)

    assert restart_signal(orders)
    assert restart_signal(posted)


async def test_an_expired_token_that_is_not_a_visitors_is_a_plain_401(
    client: AsyncClient,
) -> None:
    expired = issue_token(Principal(ANA, Role.CUSTOMER), TEST_JWT_SECRET, -60)

    response = await client.get("/orders", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401
    assert "code" not in response.json()


async def test_the_replay_is_a_recorded_conversation_about_seeded_orders(
    client: AsyncClient,
) -> None:
    replay = await client.get("/replay.json")

    assert replay.status_code == 200
    body = replay.json()
    seeded = {o["id"] for o in ORDERS if o["customer_id"] == ANA}
    assert {o["order_id"] for o in body["orders"]} == seeded
    assert body["turns"]
    for turn in body["turns"]:
        assert turn["message"] and turn["reply"]
        assert isinstance(turn["notices"], list)


async def test_orders_lists_the_visitors_orders_again(client: AsyncClient) -> None:
    visitor = await admitted(client)

    listed = await client.get("/orders", headers=bearer(visitor))

    assert listed.status_code == 200
    assert listed.json() == visitor["orders"]


async def test_demo_config_hands_the_ui_the_configured_site_key(client: AsyncClient) -> None:
    response = await client.get("/demo/config")

    assert response.status_code == 200
    assert response.json() == {"turnstile_site_key": TEST_TURNSTILE_SITE_KEY}


async def test_ui_is_served_at_the_root(client: AsyncClient) -> None:
    page = await client.get("/")
    script = await client.get("/app.js")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert '<html lang="pt-BR"' in page.text
    assert TEST_TURNSTILE_SITE_KEY not in page.text  # it comes from /demo/config
    assert script.status_code == 200


@pytest.mark.usefixtures("engine")
async def test_api_docs_work_under_the_root_path(settings: Settings, llm: ScriptedLLM) -> None:
    prefixed = settings.model_copy(update={"root_path": "/projects/support"})
    async with app_client(prefixed, llm) as client:
        # nginx strips the prefix before proxying.
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")

    assert docs.status_code == 200
    assert "/projects/support/openapi.json" in docs.text
    assert schema.status_code == 200
    assert schema.json()["servers"] == [{"url": "/projects/support"}]


@pytest.mark.usefixtures("engine")
async def test_dev_token_is_not_found_when_disabled(settings: Settings, llm: ScriptedLLM) -> None:
    production = settings.model_copy(update={"dev_token_enabled": False})
    async with app_client(production, llm) as client:
        response = await client.post("/auth/dev-token", json={"customer_id": "cus_ana"})

    assert response.status_code == 404
