"""download_tracking + grab_retry tables, one download per (candidate, hash)

Like b8d3f6a1c7e2 this runs against a database ``create_all`` may already have
touched, so each step checks first. The two new tables exist in place of columns on
``download`` so the app keeps working before this has run; only the unique index needs
the migration, and the code holds that invariant without it.

Duplicate (candidate, hash) rows are never deleted. The row worth keeping stays as it
is: a ban record first (it must keep blocking the hash), then a live download, then
imported history, then the newest. The others lose their ``external_id`` and, if still
live, become ``cancelled``, because a live row with no handle would hold its candidate
open forever.

``candidate.status`` is a plain VARCHAR with no CHECK, so ``cancelled`` needs nothing.

Revision ID: d6e2a9c4f1b3
Revises: b8d3f6a1c7e2
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d6e2a9c4f1b3"
down_revision = "b8d3f6a1c7e2"
branch_labels = None
depends_on = None

_LIVE = "('queued', 'downloading', 'importing', 'completed')"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("download_tracking"):
        op.create_table(
            "download_tracking",
            sa.Column(
                "download_id",
                sa.Integer(),
                sa.ForeignKey("download.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("last_progress", sa.Float(), nullable=False),
            sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("import_attempts", sa.Integer(), nullable=False),
            sa.Column("next_import_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not inspector.has_table("grab_retry"):
        op.create_table(
            "grab_retry",
            sa.Column(
                "candidate_id",
                sa.Integer(),
                sa.ForeignKey("candidate.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "uq_download_candidate_external" not in {
        ix["name"] for ix in inspector.get_indexes("download")
    }:
        losers = (
            "SELECT id FROM ("
            "  SELECT id, ROW_NUMBER() OVER ("
            "    PARTITION BY candidate_id, external_id"
            "    ORDER BY"
            "      CASE"
            "        WHEN error LIKE 'removed:%' THEN 0"
            f"        WHEN state IN {_LIVE} THEN 1"
            "        WHEN state = 'imported' THEN 2"
            "        ELSE 3"
            "      END,"
            "      id DESC"
            "  ) AS position FROM download WHERE external_id IS NOT NULL"
            ") WHERE position > 1"
        )
        bind.execute(
            sa.text(
                f"UPDATE download SET state = 'cancelled' "
                f"WHERE state IN {_LIVE} AND id IN ({losers})"
            )
        )
        bind.execute(sa.text(f"UPDATE download SET external_id = NULL WHERE id IN ({losers})"))
        op.create_index(
            "uq_download_candidate_external",
            "download",
            ["candidate_id", "external_id"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index("uq_download_candidate_external", table_name="download")
    op.drop_table("grab_retry")
    op.drop_table("download_tracking")
