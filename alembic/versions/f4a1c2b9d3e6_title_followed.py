"""title.followed — monitor a series and auto-grab new seasons/episodes

Revision ID: f4a1c2b9d3e6
Revises: e7b2d84f10aa
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4a1c2b9d3e6"
down_revision = "e7b2d84f10aa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("title") as batch_op:
        batch_op.add_column(
            sa.Column("followed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index(batch_op.f("ix_title_followed"), ["followed"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("title") as batch_op:
        batch_op.drop_index(batch_op.f("ix_title_followed"))
        batch_op.drop_column("followed")
