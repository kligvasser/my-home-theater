"""Engine + session factory.

For SQLite we enable WAL and a busy timeout so concurrent APScheduler jobs don't
trip ``database is locked`` (plan §5.9).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_config
from .base import Base


def _apply_sqlite_pragmas(dbapi_conn: object, _record: object) -> None:
    cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_engine(url: str, echo: bool = False) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    if url.startswith("sqlite:///"):
        # Ensure the parent directory for a file-backed SQLite DB exists.
        db_path = url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        cfg = get_config()
        _engine = create_db_engine(cfg.database.url, echo=cfg.database.echo)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session; commits on success, rolls back on error."""

    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create tables directly (dev/test). Production uses Alembic migrations."""

    Base.metadata.create_all(get_engine())
