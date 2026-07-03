"""Scan orchestration: walk a FileSystem, parse, and upsert the owned catalog.

Read-only against the NAS. Idempotent: identity is the file ``path`` and the
title natural key ``(normalized title, year, kind)``, so re-scans update in
place rather than duplicating, and files that vanished from a successfully
walked root are pruned. Each root is walked completely *before* any DB write,
and each file is committed in its own short transaction, so no transaction is
held open across slow NAS I/O and one poisoned entry (e.g. a non-UTF-8
filename) cannot sink the rest of the run. Every run is recorded as a
``job_run`` for the dashboard/history.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..db.base import utcnow
from ..db.models import JobRun, OwnedFile, RunStatus, Subtitle, Title, TitleKind
from ..db.session import session_scope
from ..errors import redact_exc
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

# SQLite's bound-parameter limit is comfortably above this.
_PRUNE_CHUNK = 500

_NON_WORD_RE = re.compile(r"[^\w\s]+")


@dataclass
class ScanStats:
    files_scanned: int = 0
    media_files: int = 0
    titles_created: int = 0
    files_added: int = 0
    files_updated: int = 0
    files_skipped: int = 0
    files_pruned: int = 0
    subtitles_found: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_text(text: str) -> str:
    """Make a path safe to log/persist: filenames can carry surrogate escapes
    that neither UTF-8 (SQLite) nor JSON (job_run.stats) accept."""

    return text.encode("utf-8", "replace").decode("utf-8")


def _is_decodable(path: str) -> bool:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _relative_to(path: str, start: str) -> str:
    """``path`` relative to the walked root, ``/``-separated, so guessit sees
    the ancestor directories (show/season) but not the root or mount prefix."""

    rel = path[len(start) :] if path.startswith(start) else path
    return rel.replace("\\", "/").strip("/")


def normalize_title(title: str) -> str:
    """Case/punctuation-insensitive form used for the title natural key, so
    ``Don't Look Up`` and ``Dont.Look.Up`` resolve to a single Title row."""

    return " ".join(_NON_WORD_RE.sub(" ", title.lower()).split())


def _get_or_create_title(session: Session, parsed: ParsedMedia) -> tuple[Title, bool]:
    """Resolve a Title by its natural key; returns ``(title, created)``.

    TMDb/IMDb ids are backfilled in Phase 2. Matching is on the normalized
    title (plus year and kind) so case/punctuation variants of the same film
    across files don't create duplicate rows.
    """

    wanted = normalize_title(parsed.title)
    existing = session.scalars(
        select(Title).where(Title.year == parsed.year, Title.kind == parsed.kind)
    )
    for candidate in existing:
        if normalize_title(candidate.title) == wanted:
            return candidate, False

    title = Title(title=parsed.title, year=parsed.year, kind=parsed.kind)
    session.add(title)
    session.flush()
    return title, True


def _upsert_owned_file(
    session: Session,
    title: Title,
    path: str,
    size: int,
    parsed: ParsedMedia,
    subtitle_langs: list[str] | None,
) -> bool:
    """Insert or refresh the row for ``path``; returns True when newly added."""

    owned = session.scalar(select(OwnedFile).where(OwnedFile.path == path))
    added = owned is None
    if owned is None:
        owned = OwnedFile(path=path, title_id=title.id, kind=parsed.kind)
        session.add(owned)

    owned.title_id = title.id
    owned.kind = parsed.kind
    owned.season = parsed.season
    owned.episode = parsed.episode
    owned.resolution = parsed.resolution
    owned.codec = parsed.codec
    owned.container = parsed.container
    owned.size_bytes = size
    owned.subtitle_langs = subtitle_langs
    return added


def _prune_missing(start: str, seen_paths: set[str], stats: ScanStats) -> None:
    """Delete owned_file rows under ``start`` whose file was not seen this walk.

    Only called after a *successful* walk, so an SMB outage never wipes the
    catalog. Titles are kept even when their last file goes away — they hold
    metadata and candidate history. Referencing subtitle rows are unlinked
    first (FK has no ON DELETE clause).
    """

    sep = "\\" if "\\" in start else "/"
    prefix = start.rstrip(sep) + sep  # trailing sep: "Movies" must not match "Movies HD"
    with session_scope() as session:
        rows = session.execute(
            select(OwnedFile.id, OwnedFile.path).where(
                OwnedFile.path.startswith(prefix, autoescape=True)
            )
        ).all()
        stale = [row_id for row_id, path in rows if path not in seen_paths]
        for i in range(0, len(stale), _PRUNE_CHUNK):
            chunk = stale[i : i + _PRUNE_CHUNK]
            session.execute(
                update(Subtitle).where(Subtitle.owned_file_id.in_(chunk)).values(owned_file_id=None)
            )
            session.execute(delete(OwnedFile).where(OwnedFile.id.in_(chunk)))
    if stale:
        stats.files_pruned += len(stale)
        log.info("scan.pruned", root=_safe_text(start), count=len(stale))


def _scan_root(
    fs: FileSystem,
    kind: TitleKind,
    root: str,
    stats: ScanStats,
) -> None:
    """Walk ``root`` fully, upsert each media file in its own transaction, then
    prune rows for files that disappeared. Raises if the walk itself fails;
    per-file problems are recorded in ``stats.errors`` and skipped."""

    start = fs.resolve(root)
    entries = list(fs.walk(root))  # finish the (slow) walk before any DB write
    stats.files_scanned += len(entries)

    subs_by_parent: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        if is_subtitle_file(entry.name):
            subs_by_parent[entry.parent].append(entry.name)

    seen_paths: set[str] = set()
    for entry in entries:
        if not is_media_file(entry.name):
            continue
        stats.media_files += 1
        if not _is_decodable(entry.path):
            stats.files_skipped += 1
            log.warning("scan.path_undecodable", path=_safe_text(entry.path))
            stats.errors.append(f"{_safe_text(entry.path)}: non-UTF-8 path; skipped")
            continue
        try:
            parsed = parse_media(_relative_to(entry.path, start), kind_hint=kind)
            if parsed is None:
                stats.files_skipped += 1
                log.warning("scan.unparsable", path=entry.path)
                continue
            langs = sorted(
                {
                    lang
                    for sub in subs_by_parent.get(entry.parent, [])
                    if (lang := subtitle_lang_for(entry.name, sub)) is not None
                }
            )
            with session_scope() as session:  # short per-file transaction
                title, created = _get_or_create_title(session, parsed)
                added = _upsert_owned_file(
                    session, title, entry.path, entry.size, parsed, langs or None
                )
            stats.titles_created += int(created)
            stats.files_added += int(added)
            stats.files_updated += int(not added)
            stats.subtitles_found += len(langs)
            seen_paths.add(entry.path)
        except Exception as exc:  # keep scanning; record the bad file
            log.warning("scan.file_failed", path=entry.path, error=redact_exc(exc))
            stats.errors.append(f"{entry.path}: {redact_exc(exc)}")

    _prune_missing(start, seen_paths, stats)


def scan_library(fs: FileSystem, roots: Mapping[TitleKind, str]) -> ScanStats:
    """Scan the given roots and upsert the catalog. Returns run statistics.

    ``roots`` maps a :class:`TitleKind` to the root path for that kind, e.g.
    ``{TitleKind.movie: "Movies", TitleKind.series: "TV Shows"}``. A root whose
    walk fails (e.g. SMB outage) is skipped without pruning and the run is
    marked failed (raising at the end), but other roots still complete and
    their results are kept.
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
        for kind, root in roots.items():
            try:
                _scan_root(fs, kind, root, stats)
            except Exception as exc:  # root-level walk/prune failure
                status = RunStatus.failed
                log.error("scan.root_failed", root=root, error=redact_exc(exc))
                stats.errors.append(f"{root}: {redact_exc(exc)}")
        log.info("scan.done", **stats.as_dict())
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(redact_exc(exc))
        log.error("scan.failed", error=redact_exc(exc))
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
