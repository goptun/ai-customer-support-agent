"""Policy questions end to end: /chat with the scripted fake LLM over the seeded Knowledge Base."""

import uuid

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.models import Ticket

from .fakes import ScriptedLLM, faq
from .test_api import ANA, chat, token_for

EXCHANGE = "Qual o prazo para trocar um produto?"


async def ask(
    client: AsyncClient, llm: ScriptedLLM, message: str, llm_reply: str | None = None
) -> dict[str, object]:
    llm.on(message, faq(), reply=llm_reply)
    response = await chat(client, await token_for(client, ANA), message)
    assert response["intent"] == "faq"
    return response


async def test_policy_question_is_answered_from_the_policy_naming_it(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    reply = str((await ask(client, llm, EXCHANGE))["reply"])

    assert "Política de Trocas" in reply
    assert "30 dias corridos" in reply


async def test_the_source_is_named_even_when_the_model_leaves_it_out(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    answer = "Você pode solicitar a troca em até 30 dias corridos a partir da data de entrega."

    reply = str((await ask(client, llm, "e o prazo de troca?", llm_reply=answer))["reply"])

    assert reply.startswith(answer)
    assert "Política de Trocas" in reply


async def test_a_reply_inventing_policy_is_replaced_by_the_policy_text(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    invented = "Segundo a Política de Trocas, você tem 60 dias para trocar."

    reply = str((await ask(client, llm, "quanto tempo tenho pra trocar?", invented))["reply"])

    assert "60 dias" not in reply
    assert "30 dias corridos" in reply
    assert "Política de Trocas" in reply


async def test_an_uncovered_question_gets_i_dont_know_and_a_ticket_offer(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    response = await ask(client, llm, "Qual a capital da França?", llm_reply="Paris.")
    reply = str(response["reply"])

    assert "Paris" not in reply
    assert "não encontrei" in reply.lower()
    assert "atendente" in reply
    async with engine.connect() as conn:
        tickets = await conn.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.conversation_id == uuid.UUID(str(response["conversation_id"])))
        )
    assert tickets == 0  # offered, not opened
