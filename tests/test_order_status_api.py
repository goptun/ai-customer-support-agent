"""Order status end to end: /chat with the scripted fake LLM and the logistics mock app."""

import pytest
from httpx import AsyncClient

from .fakes import ScriptedLLM, order_status
from .test_api import chat, token_for


async def ask(
    client: AsyncClient,
    llm: ScriptedLLM,
    customer_id: str,
    message: str,
    order_id: str | None,
    llm_reply: str | None = None,
) -> str:
    llm.on(message, order_status(order_id), reply=llm_reply)
    response = await chat(client, await token_for(client, customer_id), message)
    assert response["intent"] == "order_status"
    return str(response["reply"])


@pytest.mark.parametrize(
    ("customer_id", "order_id", "expected"),
    [
        pytest.param(
            "cus_ana",
            "1002",
            [
                "ORD-1002",
                "em trânsito",
                "Objeto em trânsito para a unidade de destino",
                "31/08/2026",
                "03/09/2026",
                "BR100000002",
            ],
            id="in_transit",
        ),
        pytest.param(
            "cus_bruno",
            "ORD-2002",
            ["atrasado", "Entrega atrasada por problema operacional", "17/08/2026", "15 dias"],
            id="delayed",
        ),
        pytest.param(
            "cus_ana",
            "ORD-1001",
            ["entregue", "Objeto entregue ao destinatário", "19/08/2026"],
            id="delivered",
        ),
        pytest.param("cus_ana", "ORD-1004", ["extraviado", "Objeto extraviado"], id="lost"),
        pytest.param(
            "cus_bruno", "ORD-2004", ["devolvido", "Objeto devolvido ao remetente"], id="returned"
        ),
        pytest.param(
            "cus_carla",
            "ORD-3003",
            ["não consegui consultar a transportadora", "04/09/2026"],
            id="timeout",
        ),
        pytest.param(
            "cus_carla",
            "ORD-3004",
            ["não consegui consultar a transportadora", "04/09/2026"],
            id="server_error",
        ),
        pytest.param(
            "cus_carla",
            "ORD-3005",
            ["ainda não tem informações de rastreio", "BR300000005", "05/09/2026"],
            id="not_found",
        ),
    ],
)
async def test_order_status_reply_for_each_logistics_scenario(
    client: AsyncClient,
    llm: ScriptedLLM,
    customer_id: str,
    order_id: str,
    expected: list[str],
) -> None:
    reply = await ask(client, llm, customer_id, f"cadê o pedido {order_id}?", order_id)

    for fact in expected:
        assert fact.lower() in reply.lower(), f"{fact!r} missing from {reply!r}"


async def test_carrier_failure_never_invents_tracking(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    reply = await ask(client, llm, "cus_carla", "e o ORD-3004?", "ORD-3004")

    for invented in ("em trânsito", "entregue", "atrasad", "extraviado", "devolvido"):
        assert invented not in reply.lower()


async def test_not_dispatched_order_is_explained_without_tracking(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    reply = await ask(client, llm, "cus_ana", "cadê meu pedido 1003?", "1003")

    assert "ainda não foi enviado" in reply
    assert "em separação" in reply
    assert "08/09/2026" in reply
    assert "rastreio" not in reply


async def test_order_of_another_customer_gets_the_same_reply_as_not_found(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    foreign = await ask(client, llm, "cus_ana", "cadê o pedido 2002?", "2002")
    unknown = await ask(client, llm, "cus_ana", "cadê o pedido 123?", "123")

    assert foreign.replace("ORD-2002", "<id>") == unknown.replace("ORD-123", "<id>")
    assert "Monitor" not in foreign and "BR200000002" not in foreign


async def test_reply_with_facts_absent_from_tool_output_is_replaced(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    hallucinated = "Seu pedido ORD-1002 foi entregue em 02/09/2026 e custou R$ 10,00."

    reply = await ask(client, llm, "cus_ana", "chegou o 1002?", "1002", llm_reply=hallucinated)

    assert "02/09/2026" not in reply
    assert "R$ 10,00" not in reply
    assert "03/09/2026" in reply


async def test_grounded_llm_reply_is_kept(client: AsyncClient, llm: ScriptedLLM) -> None:
    grounded = "Oi! O ORD-1002 está em trânsito e a previsão é 03/09/2026."

    reply = await ask(client, llm, "cus_ana", "e aí, 1002?", "1002", llm_reply=grounded)

    assert reply == grounded


async def test_order_status_without_an_order_number_asks_for_it(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    reply = await ask(client, llm, "cus_ana", "cadê meu pedido?", None)

    assert "número do pedido" in reply


async def test_status_mentioned_out_of_context_is_replaced(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    # "enviado" appears in the facts only negated ("ainda não foi enviado").
    hallucinated = "Seu pedido ORD-1003 já foi enviado e chega em 2 dias!"

    reply = await ask(client, llm, "cus_ana", "o 1003 já saiu?", "1003", llm_reply=hallucinated)

    assert "já foi enviado" not in reply
    assert "2 dias" not in reply
    assert "ainda não foi enviado" in reply
