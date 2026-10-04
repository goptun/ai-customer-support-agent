"""knowledge base chunks with pgvector embeddings

Revision ID: 9e1f7a3c5b20
Revises: 4b8d2e6f1a93
Create Date: 2026-10-03 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "9e1f7a3c5b20"
down_revision: str | Sequence[str] | None = "4b8d2e6f1a93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("document", sa.String(length=100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("section", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document", "position"),
    )
    op.create_index(
        op.f("ix_knowledge_chunks_document"), "knowledge_chunks", ["document"], unique=False
    )
    op.create_index(
        "ix_knowledge_chunks_embedding",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Downgrade schema. The vector extension is left in place."""
    op.drop_index("ix_knowledge_chunks_embedding", table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_document"), table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
