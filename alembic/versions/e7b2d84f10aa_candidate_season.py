"""candidate.season — season-scoped suggestions for owned series

Revision ID: e7b2d84f10aa
Revises: a1f3c9d27e54
Create Date: 2026-07-14

``candidate.season`` (nullable): discovery can now suggest a specific new
season of a series you already own. NULL keeps the old meaning (whole title).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7b2d84f10aa"
down_revision = "a1f3c9d27e54"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("candidate") as batch_op:
        batch_op.add_column(sa.Column("season", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("candidate") as batch_op:
        batch_op.drop_column("season")
