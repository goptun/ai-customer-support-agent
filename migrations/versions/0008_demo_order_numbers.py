"""demo order numbers

Revision ID: 5c1d7e2f8a93
Revises: 3b8f2d6a9c41
Create Date: 2026-10-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c1d7e2f8a93"
down_revision: str | Sequence[str] | None = "3b8f2d6a9c41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(sa.schema.CreateSequence(sa.Sequence("demo_order_numbers", start=100001)))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.schema.DropSequence(sa.Sequence("demo_order_numbers")))
