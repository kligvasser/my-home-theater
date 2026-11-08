"""The download_tracking migration, on the database states it can meet."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO = Path(__file__).resolve().parent.parent
PREVIOUS = "b8d3f6a1c7e2"
THIS = "d6e2a9c4f1b3"
TRASH = "a3f7c1e9d2b4"
HASH = "a" * 40


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


def _seed(db: sa.Engine, rows: list[tuple[int, str | None, str, str | None]]) -> None:
    """Rows: (id, external_id, state, error), all on one candidate."""

    with db.begin() as c:
        c.execute(sa.text("PRAGMA foreign_keys=OFF"))
        c.execute(
            sa.text(
                "INSERT INTO title (id, kind, title, followed, created_at, updated_at) "
                "VALUES (1, 'series', 'T', 0, '2026-01-01', '2026-01-01')"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO candidate (id, title_id, source, status, created_at, updated_at) "
                "VALUES (1, 1, 'discovery', 'downloading', '2026-01-01', '2026-01-01')"
            )
        )
        for row_id, external_id, state, error in rows:
            c.execute(
                sa.text(
                    "INSERT INTO download "
                    "(id, candidate_id, external_id, state, error, created_at, updated_at) "
                    "VALUES (:id, 1, :external_id, :state, :error, '2026-01-01', '2026-01-01')"
                ),
                {"id": row_id, "external_id": external_id, "state": state, "error": error},
            )


def _rows(db: sa.Engine) -> dict[int, tuple[str | None, str]]:
    with db.connect() as c:
        return {
            r[0]: (r[1], r[2])
            for r in c.execute(sa.text("SELECT id, external_id, state FROM download"))
        }


def _indexes(db: sa.Engine) -> dict[str, list[str]]:
    return {ix["name"]: ix["column_names"] for ix in sa.inspect(db).get_indexes("download")}


def test_this_revision_is_on_the_single_line() -> None:
    script = ScriptDirectory.from_config(_alembic())
    assert len(script.get_heads()) == 1
    assert script.get_revision(TRASH).down_revision == THIS


def test_upgrade_on_a_clean_database(db: sa.Engine) -> None:
    command.upgrade(_alembic(), THIS)

    tables = sa.inspect(db).get_table_names()
    assert "download_tracking" in tables and "grab_retry" in tables
    assert _indexes(db)["uq_download_candidate_external"] == ["candidate_id", "external_id"]


def test_duplicates_lose_their_handle_but_no_row_is_deleted(db: sa.Engine) -> None:
    command.upgrade(_alembic(), PREVIOUS)
    _seed(
        db,
        [
            (1, HASH, "failed", None),
            (2, HASH, "imported", None),
            (3, HASH, "downloading", None),
            (4, "b" * 40, "failed", None),
            (5, "b" * 40, "failed", None),
            (6, None, "failed", None),
            (7, None, "failed", None),
        ],
    )

    command.upgrade(_alembic(), THIS)

    assert _rows(db) == {
        1: (None, "failed"),
        2: (None, "imported"),
        3: (HASH, "downloading"),  # the live row keeps working
        4: (None, "failed"),
        5: ("b" * 40, "failed"),  # the newest of equals
        6: (None, "failed"),
        7: (None, "failed"),
    }


def test_a_ban_record_outranks_everything(db: sa.Engine) -> None:
    command.upgrade(_alembic(), PREVIOUS)
    _seed(db, [(1, HASH, "failed", "removed: malware"), (2, HASH, "downloading", None)])

    command.upgrade(_alembic(), THIS)

    # The live loser can't stay live with no handle, or it would hold its candidate
    # open forever.
    assert _rows(db) == {1: (HASH, "failed"), 2: (None, "cancelled")}


def test_runs_on_a_database_create_all_already_touched(db: sa.Engine) -> None:
    """How the live database will meet it: tables made at startup, nothing stamped."""
    from homeTheater.db import init_db

    init_db()
    with db.begin() as c:
        c.execute(sa.text("DROP INDEX uq_download_candidate_external"))
    command.stamp(_alembic(), PREVIOUS)

    command.upgrade(_alembic(), THIS)

    assert "uq_download_candidate_external" in _indexes(db)


def test_app_runs_before_the_migration(db: sa.Engine) -> None:
    """On an unmigrated database, startup's ``create_all`` adds the tables, and
    downloads still work with duplicates still there and no unique index."""
    from homeTheater.acquisition.torrent import service as svc
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import Candidate, Download, DownloadTracking

    command.upgrade(_alembic(), PREVIOUS)
    with db.begin() as c:
        c.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
    _seed(db, [(1, HASH, "failed", None), (2, HASH, "failed", None)])

    init_db()

    with session_scope() as s:
        assert s.query(Download).count() == 2
        s.add(DownloadTracking(download_id=1, last_progress_at=utcnow()))
    svc._record_grabs(1, [(HASH, "x", False)])
    with session_scope() as s:
        assert s.get(Candidate, 1).status.value == "queued"
        assert sorted(d.state for d in s.query(Download)) == ["downloading", "failed"]


def test_downgrade_removes_what_it_added(db: sa.Engine) -> None:
    command.upgrade(_alembic(), THIS)

    command.downgrade(_alembic(), PREVIOUS)

    tables = sa.inspect(db).get_table_names()
    assert "download_tracking" not in tables and "grab_retry" not in tables
    assert "uq_download_candidate_external" not in _indexes(db)


def test_trash_and_upgrade_tables_work_before_their_migration(db: sa.Engine) -> None:
    """Same promise as above: new tables only, so ``create_all`` covers a database still
    on the previous revision, and the migration then finds them already there."""

    from homeTheater.db import init_db
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import Candidate, CandidateSource, Title, TitleKind, TrashItem
    from homeTheater.db.session import session_scope

    command.upgrade(_alembic(), THIS)

    init_db()

    with session_scope() as s:
        s.add(TrashItem(original_path="a", trash_path="b", state="trashed", trashed_at=utcnow()))
        title = Title(title="T", kind=TitleKind.movie)
        s.add(title)
        s.flush()
        s.add(Candidate(title_id=title.id, source=CandidateSource.upgrade))
    with session_scope() as s:
        assert s.query(Candidate).one().source is CandidateSource.upgrade

    command.upgrade(_alembic(), TRASH)  # idempotent on what create_all made
    with session_scope() as s:
        assert s.query(TrashItem).count() == 1


def test_trash_migration_up_and_down(db: sa.Engine) -> None:
    command.upgrade(_alembic(), TRASH)
    tables = sa.inspect(db).get_table_names()
    assert "trash_item" in tables and "upgrade_check" in tables
    assert "ix_trash_item_state" in {i["name"] for i in sa.inspect(db).get_indexes("trash_item")}

    command.downgrade(_alembic(), THIS)
    tables = sa.inspect(db).get_table_names()
    assert "trash_item" not in tables and "upgrade_check" not in tables
