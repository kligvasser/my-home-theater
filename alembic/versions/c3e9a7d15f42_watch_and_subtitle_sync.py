"""watch_state, subtitle_sync and subtitle_reject tables

New tables only, so the app works on a database that hasn't run this: ``create_all``
makes them at startup. That also means they may already exist here.

Revision ID: c3e9a7d15f42
Revises: a3f7c1e9d2b4
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3e9a7d15f42"
down_revision = "a3f7c1e9d2b4"
branch_labels = None
depends_on = None


def _owned_file_fk(**kwargs: bool) -> sa.Column:
    return sa.Column(
        "owned_file_id",
        sa.Integer(),
        sa.ForeignKey("owned_file.id", ondelete="CASCADE"),
        **kwargs,
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("watch_state"):
        op.create_table(
            "watch_state",
            _owned_file_fk(primary_key=True),
            sa.Column("play_count", sa.Integer(), nullable=False),
            sa.Column("last_played_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("trakt_pushed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not inspector.has_table("subtitle_sync"):
        op.create_table(
            "subtitle_sync",
            sa.Column("id", sa.Integer(), primary_key=True),
            _owned_file_fk(nullable=False),
            sa.Column("lang", sa.String(8), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("source", sa.String(64), nullable=True),
            sa.Column("release", sa.String(512), nullable=True),
            sa.Column("result_key", sa.String(64), nullable=True),
            sa.Column("hash_match", sa.Boolean(), nullable=False),
            sa.Column("offset_seconds", sa.Float(), nullable=True),
            sa.Column("original_path", sa.String(2048), nullable=True),
            sa.Column("original_source", sa.String(64), nullable=True),
            sa.Column("original_release", sa.String(512), nullable=True),
            sa.Column("original_key", sa.String(64), nullable=True),
            sa.Column("fail_count", sa.Integer(), nullable=False),
            sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.UniqueConstraint("owned_file_id", "lang", name="uq_subtitle_sync_file_lang"),
        )
    if not inspector.has_table("subtitle_reject"):
        op.create_table(
            "subtitle_reject",
            sa.Column("id", sa.Integer(), primary_key=True),
            _owned_file_fk(nullable=False),
            sa.Column("lang", sa.String(8), nullable=False),
            sa.Column("result_key", sa.String(64), nullable=True),
            sa.Column("content_sha256", sa.String(64), nullable=True),
            sa.Column("label", sa.String(512), nullable=True),
            sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_subtitle_reject_file_lang", "subtitle_reject", ["owned_file_id", "lang"]
        )


def downgrade() -> None:
    op.drop_table("subtitle_reject")
    op.drop_table("subtitle_sync")
    op.drop_table("watch_state")
