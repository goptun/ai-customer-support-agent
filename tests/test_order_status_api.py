"""Order status end to end: /chat with the scripted fake LLM and the logistics mock app."""

import pytest
from httpx import AsyncClient

from support_agent.llm import Classification, Entities, Intent

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
    grounded = "Oi! O ORD-1002 está em trânsito (rastreio BR100000002), previsão 03/09/2026."

    reply = await ask(client, llm, "cus_ana", "e aí, 1002?", "1002", llm_reply=grounded)

    assert reply == grounded


@pytest.mark.parametrize(
    ("customer_id", "order_id", "incomplete", "missing"),
    [
        pytest.param(
            "cus_ana",
            "1002",
            "O ORD-1002 está em trânsito e chega até 03/09/2026.",
            "BR100000002",
            id="in_transit_without_tracking_code",
        ),
        pytest.param(
            "cus_bruno",
            "ORD-2002",
            "O ORD-2002 (rastreio BR200000002) está 15 dias atrasado, desculpe!",
            "17/08/2026",
            id="delayed_without_promised_date",
        ),
        pytest.param(
            "cus_ana",
            "ORD-1001",
            "Boa notícia: o ORD-1001 já foi entregue!",
            "19/08/2026",
            id="delivered_without_delivery_date",
        ),
        pytest.param(
            "cus_ana",
            "ORD-1004",
            "Olá! O rastreio do seu pedido ORD-1004 (código de rastreio BR100000004) indica que",
            "extraviado",
            id="lost_without_saying_so",
        ),
        pytest.param(
            "cus_bruno",
            "ORD-2004",
            "O ORD-2004 (rastreio BR200000004) está com a transportadora Correios.",
            "devolvido",
            id="returned_without_saying_so",
        ),
        pytest.param(
            "cus_carla",
            "ORD-3004",
            "O ORD-3004 (rastreio BR300000004) tem previsão para 04/09/2026. No momento não "
            "consegui acessar a atualização mais recente do rastreio.",
            "transportadora",
            id="carrier_error_without_blaming_the_carrier",
        ),
        pytest.param(
            "cus_carla",
            "ORD-3005",
            "O ORD-3005 (rastreio BR300000005) tem previsão para 05/09/2026. Ainda não há "
            "informações de rastreio.",
            "transportadora",
            id="no_tracking_without_naming_the_carrier",
        ),
    ],
)
async def test_reply_leaving_out_a_required_fact_is_replaced(
    client: AsyncClient,
    llm: ScriptedLLM,
    customer_id: str,
    order_id: str,
    incomplete: str,
    missing: str,
) -> None:
    reply = await ask(client, llm, customer_id, "e o meu pedido?", order_id, llm_reply=incomplete)

    assert reply != incomplete
    assert missing in reply


@pytest.mark.parametrize(
    ("customer_id", "order_id", "complete"),
    [
        pytest.param(
            "cus_ana",
            "ORD-1004",
            "Sinto muito: a transportadora informou que o ORD-1004 (BR100000004) foi extraviado.",
            id="lost_needs_the_tracking_code_and_the_loss",
        ),
        pytest.param(
            "cus_ana",
            "ORD-1004",
            "A transportadora registrou o extravio do ORD-1004 (rastreio BR100000004).",
            id="lost_may_say_extravio",
        ),
        pytest.param(
            "cus_bruno",
            "ORD-2004",
            "O ORD-2004 (rastreio BR200000004) foi devolvido ao remetente pela transportadora.",
            id="returned_needs_the_tracking_code_and_the_return",
        ),
        pytest.param(
            "cus_bruno",
            "ORD-2004",
            "A transportadora fez a devolução do ORD-2004 (rastreio BR200000004) à loja.",
            id="returned_may_say_devolucao",
        ),
        pytest.param(
            "cus_ana",
            "ORD-1003",
            "O ORD-1003 está em separação, ainda não saiu da loja. Previsão: 08/09/2026.",
            id="not_dispatched_needs_only_the_promised_date",
        ),
        pytest.param(
            "cus_carla",
            "ORD-3001",
            "O ORD-3001 está aguardando pagamento, por isso ainda não tem rastreio.",
            id="pending_payment_needs_nothing",
        ),
        pytest.param(
            "cus_carla",
            "ORD-3004",
            "Não consegui consultar a transportadora agora. O ORD-3004 (rastreio BR300000004) "
            "tem previsão para 04/09/2026.",
            id="carrier_error_needs_the_carrier",
        ),
        pytest.param(
            "cus_carla",
            "ORD-3005",
            "A transportadora ainda não tem rastreio do ORD-3005 (BR300000005). Previsão: "
            "05/09/2026.",
            id="no_tracking_needs_the_carrier",
        ),
    ],
)
async def test_reply_with_every_required_fact_is_kept(
    client: AsyncClient, llm: ScriptedLLM, customer_id: str, order_id: str, complete: str
) -> None:
    reply = await ask(client, llm, customer_id, "e o meu pedido?", order_id, llm_reply=complete)

    assert reply == complete


async def test_order_status_without_an_order_number_offers_recent_orders(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    reply = await ask(client, llm, "cus_ana", "cadê meu pedido?", None)

    assert "ORD-1002" in reply
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


async def test_asking_for_a_human_opens_a_ticket(client: AsyncClient, llm: ScriptedLLM) -> None:
    message = "quero falar com uma pessoa"
    llm.on(
        message,
        Classification(intents=[Intent.OTHER], confidence=0.9, entities=Entities(wants_human=True)),
    )

    response = await chat(client, await token_for(client, "cus_ana"), message)

    assert "chamado #" in str(response["reply"])
    assert "atendente" in str(response["reply"])
