"""Alembic environment.

Pulls the DB URL from the app config and the metadata from our models.
``render_as_batch=True`` is required for SQLite, which can't ALTER most columns
in place (plan §4 note).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from homeTheater.config import get_config

# Import models so their tables register on Base.metadata.
from homeTheater.db import (
    Base,
    models,  # noqa: F401
)
from homeTheater.db.session import create_db_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    return get_config().database.url


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_db_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
