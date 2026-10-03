"""approval requests and one pending refund per order

Revision ID: 7a3e9c1d4b58
Revises: 5d2c8a4e7f16
Create Date: 2026-10-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7a3e9c1d4b58"
down_revision: str | Sequence[str] | None = "5d2c8a4e7f16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "uq_refunds_one_pending_per_order",
        "refunds",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending_approval'"),
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("refund_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="approval_status"),
            nullable=False,
        ),
        sa.Column("policy_result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["refund_id"], ["refunds.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("refund_id"),
    )
    op.create_index(
        op.f("ix_approval_requests_customer_id"),
        "approval_requests",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_approval_requests_order_id"), "approval_requests", ["order_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_approval_requests_order_id"), table_name="approval_requests")
    op.drop_index(op.f("ix_approval_requests_customer_id"), table_name="approval_requests")
    op.drop_table("approval_requests")
    op.execute("DROP TYPE approval_status")
    op.drop_index(
        "uq_refunds_one_pending_per_order",
        table_name="refunds",
        postgresql_where=sa.text("status = 'pending_approval'"),
    )
