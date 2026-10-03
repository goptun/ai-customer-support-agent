"""Knowledge Base: the written policies, chunked by section and retrieved by embedding.

Each policy is a Markdown file in `policies/` (`# Title`, then one `## Section` per chunk).
Indexing is idempotent: a document is re-embedded only when one of its chunks changed.
"""

import asyncio
import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastembed import TextEmbedding
from pydantic import BaseModel
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.models import EMBEDDING_DIM, KnowledgeChunk

__all__ = ["EMBEDDING_DIM"]

POLICIES_DIR = Path(__file__).parent / "policies"


class Embedder(Protocol):
    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    """Local multilingual embedding model (ONNX, CPU); downloaded on first use."""

    def __init__(self, model_name: str) -> None:
        self._model = TextEmbedding(model_name)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.passage_embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()  # type: ignore[no-any-return]


@dataclass(frozen=True)
class PolicyChunk:
    document: str
    position: int
    title: str
    section: str
    content: str

    @property
    def text(self) -> str:
        """What gets embedded: the section in the context of its policy."""
        return f"{self.title} — {self.section}\n{self.content}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


class PolicyExcerpt(BaseModel):
    title: str  # the policy it comes from, named when citing it
    section: str
    content: str
    score: float  # cosine similarity to the query


def load_policies(directory: Path = POLICIES_DIR) -> list[PolicyChunk]:
    chunks = []
    for path in sorted(directory.glob("*.md")):
        head, *sections = path.read_text(encoding="utf-8").split("\n## ")
        title = head.strip().removeprefix("# ").strip()
        for position, section in enumerate(sections):
            heading, _, body = section.partition("\n")
            chunks.append(
                PolicyChunk(
                    document=path.stem,
                    position=position,
                    title=title,
                    section=heading.strip(),
                    content=body.strip(),
                )
            )
    return chunks


async def index_policies(
    sessions: async_sessionmaker[AsyncSession], embedder: Embedder, chunks: Sequence[PolicyChunk]
) -> None:
    """Make the index match `chunks`, embedding only documents that changed."""
    wanted: dict[str, set[tuple[int, str]]] = defaultdict(set)
    for chunk in chunks:
        wanted[chunk.document].add((chunk.position, chunk.content_hash))

    async with sessions() as session:
        rows = await session.execute(
            select(KnowledgeChunk.document, KnowledgeChunk.position, KnowledgeChunk.content_hash)
        )
    indexed: dict[str, set[tuple[int, str]]] = defaultdict(set)
    for document, position, content_hash in rows:
        indexed[document].add((position, content_hash))

    outdated = {doc for doc in indexed.keys() | wanted.keys() if indexed[doc] != wanted[doc]}
    if not outdated:
        return
    fresh = [chunk for chunk in chunks if chunk.document in outdated]
    embeddings = await asyncio.to_thread(embedder.embed_passages, [c.text for c in fresh])

    async with sessions.begin() as session:
        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document.in_(outdated)))
        if fresh:
            await session.execute(
                insert(KnowledgeChunk),
                [
                    {
                        "document": chunk.document,
                        "position": chunk.position,
                        "title": chunk.title,
                        "section": chunk.section,
                        "content": chunk.content,
                        "content_hash": chunk.content_hash,
                        "embedding": embedding,
                    }
                    for chunk, embedding in zip(fresh, embeddings, strict=True)
                ],
            )


class KnowledgeBase:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        top_k: int,
        min_score: float,
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._top_k = top_k
        self._min_score = min_score

    async def search(self, query: str) -> list[PolicyExcerpt]:
        """The top-k excerpts closest to `query`, best first, leaving out irrelevant ones."""
        vector = await asyncio.to_thread(self._embedder.embed_query, query)
        distance = KnowledgeChunk.embedding.cosine_distance(vector)
        async with self._sessions() as session:
            rows = await session.execute(
                select(
                    KnowledgeChunk.title,
                    KnowledgeChunk.section,
                    KnowledgeChunk.content,
                    distance.label("distance"),
                )
                .order_by(distance)
                .limit(self._top_k)
            )
        excerpts = [
            PolicyExcerpt(
                title=row.title, section=row.section, content=row.content, score=1 - row.distance
            )
            for row in rows
        ]
        return [excerpt for excerpt in excerpts if excerpt.score >= self._min_score]
