"""subtitle_attempt negative cache + one subtitle row per (owned file, language)

Written to run against a database that ``create_all`` already touched (the live DB
isn't reliably Alembic-managed): each step checks what is there first. Older
duplicate subtitle rows are unlinked (``owned_file_id = NULL``), the same thing the
scanner does for a pruned file, so no row is deleted.

Revision ID: b8d3f6a1c7e2
Revises: f4a1c2b9d3e6
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d3f6a1c7e2"
down_revision = "f4a1c2b9d3e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("subtitle_attempt"):
        op.create_table(
            "subtitle_attempt",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "owned_file_id",
                sa.Integer(),
                sa.ForeignKey("owned_file.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("lang", sa.String(length=8), nullable=False),
            sa.Column("last_tried", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fail_count", sa.Integer(), nullable=False),
            sa.Column("last_outcome", sa.String(length=32), nullable=True),
            sa.UniqueConstraint("owned_file_id", "lang", name="uq_subtitle_attempt_file_lang"),
        )

    if "uq_subtitle_file_lang" not in {ix["name"] for ix in inspector.get_indexes("subtitle")}:
        # Keep the newest row of each (file, language); unlink the rest.
        bind.execute(
            sa.text(
                "UPDATE subtitle SET owned_file_id = NULL "
                "WHERE owned_file_id IS NOT NULL AND id NOT IN ("
                "  SELECT MAX(id) FROM subtitle "
                "  WHERE owned_file_id IS NOT NULL GROUP BY owned_file_id, lang"
                ")"
            )
        )
        op.create_index("uq_subtitle_file_lang", "subtitle", ["owned_file_id", "lang"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_subtitle_file_lang", table_name="subtitle")
    op.drop_table("subtitle_attempt")
