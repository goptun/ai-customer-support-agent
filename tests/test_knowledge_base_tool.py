"""search_knowledge_base and Knowledge Base indexing against real Postgres."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.db import session_factory
from support_agent.knowledge import KnowledgeBase, index_policies, load_policies
from support_agent.models import KnowledgeChunk
from support_agent.tools import (
    ErrorCode,
    KnowledgeBaseResult,
    ToolError,
    search_knowledge_base,
)

from .conftest import KNOWLEDGE_MIN_SCORE
from .fakes import LexicalEmbedder


def knowledge_base(engine: AsyncEngine) -> KnowledgeBase:
    return KnowledgeBase(
        session_factory(engine), LexicalEmbedder(), top_k=3, min_score=KNOWLEDGE_MIN_SCORE
    )


async def indexed(engine: AsyncEngine) -> list[tuple[str, int, str]]:
    async with session_factory(engine)() as session:
        rows = await session.execute(
            select(
                KnowledgeChunk.document, KnowledgeChunk.position, KnowledgeChunk.content_hash
            ).order_by(KnowledgeChunk.document, KnowledgeChunk.position)
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


def test_the_seed_has_about_ten_policy_documents() -> None:
    chunks = load_policies()

    titles = {chunk.title for chunk in chunks}
    assert 8 <= len(titles) <= 12
    assert {"Política de Entrega", "Política de Trocas", "Política de Reembolso"} <= titles
    assert all(chunk.section and chunk.content for chunk in chunks)


async def test_search_returns_top_k_chunks_with_source_titles(engine: AsyncEngine) -> None:
    result = await search_knowledge_base(
        knowledge_base(engine), {"query": "Qual o prazo para trocar um produto?"}
    )

    assert isinstance(result, KnowledgeBaseResult)
    assert 1 <= len(result.chunks) <= 3
    top = result.chunks[0]
    assert top.title == "Política de Trocas"
    assert top.section == "Prazo para troca"
    assert "30 dias" in top.content
    scores = [chunk.score for chunk in result.chunks]
    assert scores == sorted(scores, reverse=True)


async def test_no_chunk_is_returned_when_nothing_is_relevant(engine: AsyncEngine) -> None:
    result = await search_knowledge_base(
        knowledge_base(engine), {"query": "Qual a capital da França?"}
    )

    assert isinstance(result, KnowledgeBaseResult)
    assert result.chunks == []


@pytest.mark.parametrize("args", [{}, {"query": "   "}, {"query": 42}])
async def test_invalid_query_is_a_structured_error(
    engine: AsyncEngine, args: dict[str, object]
) -> None:
    result = await search_knowledge_base(knowledge_base(engine), args)

    assert result == ToolError(code=ErrorCode.INVALID_ARGUMENT)


async def test_indexing_again_changes_nothing_and_embeds_nothing(engine: AsyncEngine) -> None:
    before = await indexed(engine)
    embedder = LexicalEmbedder()

    await index_policies(session_factory(engine), embedder, load_policies())

    assert await indexed(engine) == before
    assert embedder.passages_embedded == 0


async def test_only_a_changed_document_is_reindexed(engine: AsyncEngine) -> None:
    sessions = session_factory(engine)
    policies = load_policies()
    before = await indexed(engine)
    changed = [
        replace(chunk, content="Trocas em até 45 dias corridos.")
        if chunk.title == "Política de Trocas" and chunk.position == 0
        else chunk
        for chunk in policies
    ]
    embedder = LexicalEmbedder()
    try:
        await index_policies(sessions, embedder, changed)

        trocas = sum(chunk.title == "Política de Trocas" for chunk in policies)
        assert embedder.passages_embedded == trocas
        hits = await knowledge_base(engine).search("Trocas em até 45 dias corridos")
        assert hits[0].content == "Trocas em até 45 dias corridos."
    finally:
        await index_policies(sessions, LexicalEmbedder(), policies)
    assert await indexed(engine) == before


async def test_a_removed_document_is_dropped_from_the_index(engine: AsyncEngine) -> None:
    sessions = session_factory(engine)
    policies = load_policies()
    try:
        await index_policies(
            sessions, LexicalEmbedder(), [c for c in policies if c.title != "Política de Frete"]
        )

        async with sessions() as session:
            count = await session.scalar(
                select(func.count()).where(KnowledgeChunk.title == "Política de Frete")
            )
        assert count == 0
    finally:
        await index_policies(sessions, LexicalEmbedder(), policies)
