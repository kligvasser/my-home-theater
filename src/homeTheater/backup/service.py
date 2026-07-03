"""SQLite database backup (plan §11: persist + periodically back up the DB).

Uses SQLite's online backup API so it is safe to run against a live DB (WAL
included). Keeps the newest ``keep`` snapshots and prunes the rest.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import AppConfig
from ..db.base import utcnow
from ..logging_setup import get_logger

log = get_logger(__name__)


def _sqlite_path(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise ValueError(f"DB backup only supports file-backed SQLite, got {url!r}")
    path = url.removeprefix("sqlite:///")
    if not path or path == ":memory:":
        raise ValueError("Cannot back up an in-memory SQLite database")
    return Path(path)


def backup_database(config: AppConfig, dest_dir: str | Path | None = None, keep: int = 7) -> Path:
    """Snapshot the SQLite DB to a timestamped file and prune old backups."""

    src = _sqlite_path(config.database.url)
    out_dir = Path(dest_dir) if dest_dir else src.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"{src.stem}-{stamp}.db"

    with sqlite3.connect(str(src)) as source, sqlite3.connect(str(dest)) as target:
        source.backup(target)

    backups = sorted(out_dir.glob(f"{src.stem}-*.db"))
    for old in backups[: max(0, len(backups) - keep)]:
        old.unlink(missing_ok=True)

    log.info("backup.done", dest=str(dest), kept=min(len(backups), keep))
    return dest
