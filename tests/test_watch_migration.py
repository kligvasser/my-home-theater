"""The watch_state / subtitle_sync / subtitle_reject migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO = Path(__file__).resolve().parent.parent
THIS = "c3e9a7d15f42"
TABLES = {"watch_state", "subtitle_sync", "subtitle_reject"}


def _alembic() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    return cfg


def _previous() -> str:
    previous = ScriptDirectory.from_config(_alembic()).get_revision(THIS).down_revision
    assert isinstance(previous, str)
    return previous


@pytest.fixture
def db(config_file: Path, tmp_path: Path) -> sa.Engine:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    return sa.create_engine(f"sqlite:///{tmp_path / 'test.db'}")


def test_upgrade_makes_tables_matching_the_models(db: sa.Engine) -> None:
    from homeTheater.db.base import Base

    command.upgrade(_alembic(), THIS)

    inspector = sa.inspect(db)
    for name in TABLES:
        columns = {c["name"] for c in inspector.get_columns(name)}
        assert columns == set(Base.metadata.tables[name].columns.keys())


def test_upgrade_over_tables_create_all_already_made(db: sa.Engine) -> None:
    from homeTheater.db import init_db

    command.upgrade(_alembic(), _previous())
    init_db()

    command.upgrade(_alembic(), THIS)

    assert set(sa.inspect(db).get_table_names()) >= TABLES


def test_app_runs_before_the_migration(db: sa.Engine) -> None:
    """Startup's ``create_all`` adds the tables to an unmigrated database."""

    from homeTheater.db import init_db, session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import (
        OwnedFile,
        SubtitleReject,
        SubtitleSync,
        Title,
        TitleKind,
        WatchState,
    )

    command.upgrade(_alembic(), _previous())
    with db.begin() as c:
        c.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))

    init_db()

    now = utcnow()
    with session_scope() as s:
        title = Title(title="t", kind=TitleKind.movie)
        s.add(title)
        s.flush()
        owned = OwnedFile(title_id=title.id, path="/a", kind=TitleKind.movie)
        s.add(owned)
        s.flush()
        s.add(WatchState(owned_file_id=owned.id, play_count=1, source="kodi", updated_at=now))
        s.add(SubtitleSync(owned_file_id=owned.id, lang="en", fail_count=0))
        s.add(SubtitleReject(owned_file_id=owned.id, lang="en", rejected_at=now))


def test_downgrade_removes_what_it_added(db: sa.Engine) -> None:
    command.upgrade(_alembic(), THIS)

    command.downgrade(_alembic(), _previous())

    assert not TABLES & set(sa.inspect(db).get_table_names())
