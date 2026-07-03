"""kind-scoped tmdb ids + taste/ML feature columns

Revision ID: a1f3c9d27e54
Revises: c25bff6e1243
Create Date: 2026-07-03

* ``title.tmdb_id`` uniqueness becomes ``(tmdb_id, kind)`` — TMDb movie and TV
  ids are independent sequences, so a movie and a series may share an id.
* Taste/ML feature columns on ``title`` (language, countries, certification,
  keywords, cast, directors, collection, series shape) + ``arr_has_file`` +
  ``last_enriched_at`` bookkeeping.
* ``candidate.features`` — feature snapshot at decision time (training data).
* ``owned_file.episode_end`` — multi-episode files (S01E01E02).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1f3c9d27e54"
down_revision = "c25bff6e1243"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("title") as batch_op:
        batch_op.drop_index(batch_op.f("ix_title_tmdb_id"))
        batch_op.create_index(batch_op.f("ix_title_tmdb_id"), ["tmdb_id"], unique=False)
        batch_op.create_unique_constraint("uq_title_tmdb_kind", ["tmdb_id", "kind"])
        batch_op.add_column(sa.Column("original_language", sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column("origin_countries", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("release_date", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("certification", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("keywords", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("cast_top", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("directors", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("collection_tmdb_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("collection_name", sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column("seasons_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("episodes_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("series_status", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column(
                "arr_has_file",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("last_enriched_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("owned_file") as batch_op:
        batch_op.add_column(sa.Column("episode_end", sa.Integer(), nullable=True))

    with op.batch_alter_table("candidate") as batch_op:
        batch_op.add_column(sa.Column("features", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("candidate") as batch_op:
        batch_op.drop_column("features")

    with op.batch_alter_table("owned_file") as batch_op:
        batch_op.drop_column("episode_end")

    with op.batch_alter_table("title") as batch_op:
        batch_op.drop_column("last_enriched_at")
        batch_op.drop_column("arr_has_file")
        batch_op.drop_column("series_status")
        batch_op.drop_column("episodes_count")
        batch_op.drop_column("seasons_count")
        batch_op.drop_column("collection_name")
        batch_op.drop_column("collection_tmdb_id")
        batch_op.drop_column("directors")
        batch_op.drop_column("cast_top")
        batch_op.drop_column("keywords")
        batch_op.drop_column("certification")
        batch_op.drop_column("release_date")
        batch_op.drop_column("origin_countries")
        batch_op.drop_column("original_language")
        batch_op.drop_constraint("uq_title_tmdb_kind", type_="unique")
        batch_op.drop_index(batch_op.f("ix_title_tmdb_id"))
        batch_op.create_index(batch_op.f("ix_title_tmdb_id"), ["tmdb_id"], unique=True)
