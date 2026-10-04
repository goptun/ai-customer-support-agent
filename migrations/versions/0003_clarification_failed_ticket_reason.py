"""clarification_failed ticket reason

Revision ID: 4b8d2e6f1a93
Revises: 7c3e51b9d0a2
Create Date: 2026-10-03 21:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b8d2e6f1a93"
down_revision: str | Sequence[str] | None = "7c3e51b9d0a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE ticket_reason ADD VALUE IF NOT EXISTS 'clarification_failed'")


def downgrade() -> None:
    """Downgrade schema.

    Postgres can't drop an enum value, so the type is rebuilt; the cast fails (and nothing
    changes) while any Ticket still has the removed reason.
    """
    op.execute("ALTER TYPE ticket_reason RENAME TO ticket_reason_old")
    op.execute("CREATE TYPE ticket_reason AS ENUM ('customer_requested', 'tool_failure')")
    op.execute(
        "ALTER TABLE tickets ALTER COLUMN reason TYPE ticket_reason "
        "USING reason::text::ticket_reason"
    )
    op.execute("DROP TYPE ticket_reason_old")
