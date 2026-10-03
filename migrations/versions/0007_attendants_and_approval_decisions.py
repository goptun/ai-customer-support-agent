"""attendants and approval decisions

Revision ID: 3b8f2d6a9c41
Revises: 7a3e9c1d4b58
Create Date: 2026-10-04 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b8f2d6a9c41"
down_revision: str | Sequence[str] | None = "7a3e9c1d4b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "attendants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("approval_requests", sa.Column("decided_by", sa.String(length=64), nullable=True))
    op.add_column(
        "approval_requests", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("approval_requests", sa.Column("decision_reason", sa.Text(), nullable=True))
    op.add_column(
        "approval_requests", sa.Column("relayed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "approval_requests", sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_approval_requests_decided_by",
        "approval_requests",
        "attendants",
        ["decided_by"],
        ["id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("fk_approval_requests_decided_by", "approval_requests", type_="foreignkey")
    op.drop_column("approval_requests", "seen_at")
    op.drop_column("approval_requests", "relayed_at")
    op.drop_column("approval_requests", "decision_reason")
    op.drop_column("approval_requests", "decided_at")
    op.drop_column("approval_requests", "decided_by")
    op.drop_table("attendants")
