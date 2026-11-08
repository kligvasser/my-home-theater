"""The subtitle_attempt migration, on the database states it can meet."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO = Path(__file__).resolve().parent.parent
PREVIOUS = "f4a1c2b9d3e6"
THIS = "b8d3f6a1c7e2"


def _alembic() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    return cfg


@pytest.fixture
def db(config_file: Path, tmp_path: Path) -> sa.Engine:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    return sa.create_engine(f"sqlite:///{tmp_path / 'test.db'}")


def _seed_subtitles(db: sa.Engine, rows: list[tuple[int, int | None, str]]) -> None:
    with db.begin() as c:
        c.execute(sa.text("PRAGMA foreign_keys=OFF"))
        c.execute(
            sa.text(
                "INSERT INTO title (id, kind, title, followed, created_at, updated_at) "
                "VALUES (1, 'movie', 'T', 0, '2026-01-01', '2026-01-01')"
            )
        )
        for owned_id in sorted({r[1] for r in rows if r[1] is not None}):
            c.execute(
                sa.text(
                    "INSERT INTO owned_file (id, title_id, path, kind, created_at, updated_at) "
                    "VALUES (:id, 1, :path, 'movie', '2026-01-01', '2026-01-01')"
                ),
                {"id": owned_id, "path": f"/f{owned_id}"},
            )
        for row_id, owned_id, lang in rows:
            c.execute(
                sa.text(
                    "INSERT INTO subtitle (id, title_id, owned_file_id, lang, created_at, "
                    "updated_at) VALUES (:id, 1, :owned, :lang, '2026-01-01', '2026-01-01')"
                ),
                {"id": row_id, "owned": owned_id, "lang": lang},
            )


def test_this_revision_is_on_the_single_line() -> None:
    script = ScriptDirectory.from_config(_alembic())
    assert len(script.get_heads()) == 1
    assert script.get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_keeps_the_newest_duplicate_and_deletes_nothing(db: sa.Engine) -> None:
    command.upgrade(_alembic(), PREVIOUS)
    _seed_subtitles(db, [(1, 7, "he"), (2, 7, "he"), (3, 7, "en"), (4, None, "he")])

    command.upgrade(_alembic(), THIS)

    with db.connect() as c:
        rows = c.execute(sa.text("SELECT id, owned_file_id FROM subtitle ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [(1, None), (2, 7), (3, 7), (4, None)]
        with pytest.raises(sa.exc.IntegrityError):
            c.execute(
                sa.text(
                    "INSERT INTO subtitle (title_id, owned_file_id, lang, created_at, updated_at)"
                    " VALUES (1, 7, 'he', '2026-01-01', '2026-01-01')"
                )
            )


def test_upgrade_tolerates_tables_create_all_already_made(db: sa.Engine) -> None:
    """The live path: ``init_db`` on startup creates the table before any migration."""

    from homeTheater.db import Base, models  # noqa: F401

    Base.metadata.create_all(db)
    command.stamp(_alembic(), PREVIOUS)

    command.upgrade(_alembic(), THIS)

    assert sa.inspect(db).has_table("subtitle_attempt")


def test_upgrade_adds_the_index_create_all_cannot_add(db: sa.Engine) -> None:
    """``create_all`` makes the new table, but never touches the existing ``subtitle``."""

    from homeTheater.db import Base, models  # noqa: F401

    command.upgrade(_alembic(), PREVIOUS)
    Base.metadata.create_all(db)
    names = {ix["name"] for ix in sa.inspect(db).get_indexes("subtitle")}
    assert "uq_subtitle_file_lang" not in names

    command.upgrade(_alembic(), THIS)

    names = {ix["name"] for ix in sa.inspect(db).get_indexes("subtitle")}
    assert "uq_subtitle_file_lang" in names


def test_migrated_schema_matches_the_models(db: sa.Engine) -> None:
    from homeTheater.db import models

    command.upgrade(_alembic(), THIS)

    inspector = sa.inspect(db)
    columns = {c["name"]: c for c in inspector.get_columns("subtitle_attempt")}
    assert set(columns) == {c.name for c in models.SubtitleAttempt.__table__.columns}
    assert not columns["fail_count"]["nullable"] and columns["last_outcome"]["nullable"]
    (fk,) = inspector.get_foreign_keys("subtitle_attempt")
    assert fk["referred_table"] == "owned_file" and fk["options"]["ondelete"] == "CASCADE"


def test_downgrade_removes_both(db: sa.Engine) -> None:
    command.upgrade(_alembic(), THIS)

    command.downgrade(_alembic(), PREVIOUS)

    inspector = sa.inspect(db)
    assert not inspector.has_table("subtitle_attempt")
    assert "uq_subtitle_file_lang" not in {ix["name"] for ix in inspector.get_indexes("subtitle")}
