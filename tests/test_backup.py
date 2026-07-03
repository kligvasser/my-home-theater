"""Backup service: missing-source guard, snapshot correctness, strict pruning."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from homeTheater.errors import NotConfiguredError


def _reset() -> None:
    from homeTheater.config import loader

    loader.get_config.cache_clear()


def test_backup_missing_source_raises(config_file: Path, tmp_path: Path) -> None:
    """A wrong database.url/cwd must fail loudly, not create an empty DB and
    silently 'back up' nothing."""

    _reset()
    from homeTheater.backup.service import backup_database
    from homeTheater.config import get_config

    src = tmp_path / "test.db"  # config_file points database.url here; never created
    dest_dir = tmp_path / "backups"

    with pytest.raises(NotConfiguredError, match="not found"):
        backup_database(get_config(), dest_dir=dest_dir)

    assert not src.exists()  # sqlite3.connect must not have created an empty DB
    assert not dest_dir.exists()


def test_backup_snapshots_and_prunes(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    import homeTheater.backup.service as svc
    from homeTheater.config import get_config

    src = tmp_path / "test.db"
    with closing(sqlite3.connect(str(src))) as conn, conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")

    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    unrelated = dest_dir / "test-notes.db"  # matches a naive "test-*.db" glob
    unrelated.write_text("keep me")

    base = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
    ticks = (base + timedelta(seconds=i) for i in range(10))
    monkeypatch.setattr(svc, "utcnow", lambda: next(ticks))

    paths = [svc.backup_database(get_config(), dest_dir=dest_dir, keep=2) for _ in range(3)]

    # Oldest snapshot pruned, newest two kept, unrelated file untouched.
    assert not paths[0].exists()
    assert paths[1].exists() and paths[2].exists()
    assert unrelated.read_text() == "keep me"

    with closing(sqlite3.connect(str(paths[-1]))) as check:
        assert check.execute("SELECT x FROM t").fetchone() == (42,)
