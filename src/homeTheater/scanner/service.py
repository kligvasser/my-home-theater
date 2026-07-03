"""Scan orchestration: walk a FileSystem, parse, and upsert the owned catalog.

Read-only against the NAS. Idempotent: identity is the file ``path`` and the
title natural key ``(title, year, kind)``, so re-scans update in place rather than
duplicating. Every run is recorded as a ``job_run`` for the dashboard/history.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.base import utcnow
from ..db.models import JobRun, OwnedFile, RunStatus, Title, TitleKind
from ..db.session import session_scope
from ..logging_setup import bind_run, clear_run, get_logger
from .filesystem import FileSystem
from .parse import (
    ParsedMedia,
    is_media_file,
    is_subtitle_file,
    parse_media,
    subtitle_lang_for,
)

log = get_logger(__name__)


@dataclass
class ScanStats:
    files_scanned: int = 0
    media_files: int = 0
    titles_created: int = 0
    files_added: int = 0
    files_updated: int = 0
    subtitles_found: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get_or_create_title(session: Session, parsed: ParsedMedia, stats: ScanStats) -> Title:
    """Resolve a Title by its natural key. TMDb/IMDb ids are backfilled in Phase 2."""

    stmt = select(Title).where(
        Title.title == parsed.title,
        Title.year == parsed.year,
        Title.kind == parsed.kind,
    )
    title = session.scalar(stmt)
    if title is None:
        title = Title(title=parsed.title, year=parsed.year, kind=parsed.kind)
        session.add(title)
        session.flush()
        stats.titles_created += 1
    return title


def _upsert_owned_file(
    session: Session,
    title: Title,
    path: str,
    size: int,
    parsed: ParsedMedia,
    subtitle_langs: list[str] | None,
    stats: ScanStats,
) -> None:
    owned = session.scalar(select(OwnedFile).where(OwnedFile.path == path))
    if owned is None:
        owned = OwnedFile(path=path, title_id=title.id, kind=parsed.kind)
        session.add(owned)
        stats.files_added += 1
    else:
        stats.files_updated += 1

    owned.title_id = title.id
    owned.kind = parsed.kind
    owned.season = parsed.season
    owned.episode = parsed.episode
    owned.resolution = parsed.resolution
    owned.codec = parsed.codec
    owned.container = parsed.container
    owned.size_bytes = size
    owned.subtitle_langs = subtitle_langs


def _scan_root(
    session: Session,
    fs: FileSystem,
    kind: TitleKind,
    root: str,
    stats: ScanStats,
) -> None:
    entries = list(fs.walk(root))
    stats.files_scanned += len(entries)

    subs_by_parent: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        if is_subtitle_file(entry.name):
            subs_by_parent[entry.parent].append(entry.name)

    for entry in entries:
        if not is_media_file(entry.name):
            continue
        stats.media_files += 1
        try:
            parsed = parse_media(entry.name, kind_hint=kind)
            langs = sorted(
                {
                    lang
                    for sub in subs_by_parent.get(entry.parent, [])
                    if (lang := subtitle_lang_for(entry.name, sub)) is not None
                }
            )
            stats.subtitles_found += len(langs)
            title = _get_or_create_title(session, parsed, stats)
            _upsert_owned_file(session, title, entry.path, entry.size, parsed, langs or None, stats)
        except Exception as exc:  # keep scanning; record the bad file
            log.warning("scan.file_failed", path=entry.path, error=str(exc))
            stats.errors.append(f"{entry.path}: {exc}")


def scan_library(fs: FileSystem, roots: Mapping[TitleKind, str]) -> ScanStats:
    """Scan the given roots and upsert the catalog. Returns run statistics.

    ``roots`` maps a :class:`TitleKind` to the root path for that kind, e.g.
    ``{TitleKind.movie: "Movies", TitleKind.series: "TV Shows"}``.
    """

    # 1. Open the run row (committed immediately so history reflects in-progress).
    with session_scope() as session:
        run = JobRun(kind="scan", started_at=utcnow(), status=RunStatus.running)
        session.add(run)
        session.flush()
        run_id = run.id

    bind_run(run_id=run_id, job="scan")
    stats = ScanStats()
    status = RunStatus.success
    try:
        log.info("scan.start", roots={k.value: v for k, v in roots.items()})
        with session_scope() as session:
            for kind, root in roots.items():
                _scan_root(session, fs, kind, root, stats)
        log.info("scan.done", **stats.as_dict())
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(str(exc))
        log.error("scan.failed", error=str(exc))
    finally:
        with session_scope() as session:
            job_run = session.get(JobRun, run_id)
            if job_run is not None:
                job_run.finished_at = utcnow()
                job_run.status = status
                job_run.stats = stats.as_dict()
        clear_run()

    if status is RunStatus.failed:
        raise RuntimeError(f"Scan failed: {stats.errors}")
    return stats
