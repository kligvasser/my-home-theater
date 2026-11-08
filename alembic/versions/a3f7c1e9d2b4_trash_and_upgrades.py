"""trash_item + upgrade_check tables

New tables only, so ``create_all`` makes them and the app works before this runs;
each step checks first for the same reason. ``candidate.source`` gains the value
``upgrade`` with no schema change: it's a plain VARCHAR with no CHECK.

Revision ID: a3f7c1e9d2b4
Revises: d6e2a9c4f1b3
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f7c1e9d2b4"
down_revision = "d6e2a9c4f1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("trash_item"):
        op.create_table(
            "trash_item",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("original_path", sa.String(2048), nullable=False),
            sa.Column("trash_path", sa.String(2048), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("trash_item")}
    if "ix_trash_item_state" not in indexes:
        op.create_index("ix_trash_item_state", "trash_item", ["state"])
    if not inspector.has_table("upgrade_check"):
        op.create_table(
            "upgrade_check",
            sa.Column(
                "title_id",
                sa.Integer(),
                sa.ForeignKey("title.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("upgrade_check")
    op.drop_index("ix_trash_item_state", table_name="trash_item")
    op.drop_table("trash_item")
