"""Scan orchestration: walk a FileSystem, parse, and upsert the owned catalog.

Read-only against the NAS. Idempotent: identity is the file ``path`` and the
title natural key ``(normalized title, year, kind)``, so re-scans update in
place rather than duplicating, and files that vanished from a successfully
walked root are pruned, within the limits in :class:`Scanner` settings. Each root
is walked completely *before* any DB write,
and each file is committed in its own short transaction, so no transaction is
held open across slow NAS I/O and one poisoned entry (e.g. a non-UTF-8
filename) cannot sink the rest of the run. Every run is recorded as a
``job_run`` for the dashboard/history.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..config.settings import Scanner
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
    subtitle_lang_standalone,
)

log = get_logger(__name__)

# Subtitle subfolder names (case-insensitive): Movie/Subs/, Season 01/Subs/ ...
SUBS_DIR_NAMES = frozenset({"subs", "sub", "subtitles"})

# SQLite's bound-parameter limit is comfortably above this.
_PRUNE_CHUNK = 500
# How many of a refused prune's paths the job_run keeps, for the inbox to show.
_PRUNE_SAMPLE = 10

_NON_WORD_RE = re.compile(r"[^\w\s]+")


class PruneRefusedError(RuntimeError):
    """A walk would remove too much of a root to be believed. Its rows are kept."""


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
    # What each refused prune would have removed, for the inbox's review.
    prune_refused: list[dict[str, Any]] = field(default_factory=list)

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


def _show_folder_title(session: Session, prefix: str) -> Title | None:
    """The title most files under a show folder already belong to.

    The folder decides identity, but its name need not match the catalog: enrichment
    renames titles to TMDb's spelling (folder ``Better Call Soul`` is ``Better Call
    Saul`` here), so a natural-key lookup would split the show's next episode off.
    """

    title_id = session.scalar(
        select(OwnedFile.title_id)
        .where(OwnedFile.path.startswith(prefix, autoescape=True))
        .group_by(OwnedFile.title_id)
        .order_by(func.count().desc(), OwnedFile.title_id)
        .limit(1)
    )
    return None if title_id is None else session.get(Title, title_id)


def _get_or_create_title(
    session: Session, parsed: ParsedMedia, folder_prefix: str | None = None
) -> tuple[Title, bool]:
    """Resolve a Title by its natural key; returns ``(title, created)``.

    TMDb/IMDb ids are backfilled in Phase 2. Matching is on the normalized
    title (plus year and kind) so case/punctuation variants of the same film
    across files don't create duplicate rows.

    ``folder_prefix`` is the path of the series folder that named this file; files
    already under it settle the question. A series name otherwise ignores a missing
    year on either side, as folders usually have none and enrichment fills it in
    (the year can still tell ``Battlestar Galactica (2004)`` from its 1978 namesake).
    """

    if folder_prefix is not None:
        owner = _show_folder_title(session, folder_prefix)
        if owner is not None:
            return owner, False

    wanted = normalize_title(parsed.title)
    same_kind = session.scalars(select(Title).where(Title.kind == parsed.kind)).all()
    named = [t for t in same_kind if normalize_title(t.title) == wanted]
    for candidate in named:
        if candidate.year == parsed.year:
            return candidate, False
    if parsed.kind is TitleKind.series:
        undated = [t for t in named if parsed.year is None or t.year is None]
        if len(undated) == 1:
            return undated[0], False

    title = Title(title=parsed.title, year=parsed.year, kind=parsed.kind)
    session.add(title)
    session.flush()
    return title, True


def _upsert_owned_file(
    session: Session,
    path: str,
    size: int,
    parsed: ParsedMedia,
    subtitle_langs: list[str] | None,
    *,
    walk_started: datetime | None = None,
    folder_prefix: str | None = None,
) -> tuple[bool, bool]:
    """Insert or refresh the row for ``path``; returns (added, title_created).

    A path already in the catalog KEEPS its title link: enrichment renames
    titles to their canonical TMDb form, so re-resolving by parsed filename
    would repoint files onto fresh duplicate titles and orphan the enriched
    rows. Only genuinely new paths resolve (or create) a title.

    ``subtitle_langs`` is what the walk saw, which can already be out of date: a
    subtitle stored since ``walk_started`` keeps its language.
    """

    owned = session.scalar(select(OwnedFile).where(OwnedFile.path == path))
    added = owned is None
    created = False
    if owned is None:
        title, created = _get_or_create_title(session, parsed, folder_prefix)
        owned = OwnedFile(path=path, title_id=title.id, kind=parsed.kind)
        session.add(owned)

    owned.kind = parsed.kind
    owned.season = parsed.season
    owned.episode = parsed.episode
    owned.episode_end = parsed.episode_end
    owned.resolution = parsed.resolution
    owned.codec = parsed.codec
    owned.container = parsed.container
    owned.size_bytes = size
    if not added and walk_started is not None:
        stored_since = session.scalars(
            select(Subtitle.lang).where(
                Subtitle.owned_file_id == owned.id, Subtitle.updated_at >= walk_started
            )
        ).all()
        subtitle_langs = sorted({*(subtitle_langs or []), *stored_since}) or None
    owned.subtitle_langs = subtitle_langs
    return added, created


def _check_prune_allowed(*, stale: int, total: int, walked: int, limits: Scanner) -> None:
    """Raise :class:`PruneRefusedError` if removing ``stale`` of ``total`` rows
    looks like a NAS problem rather than files someone really deleted.

    ``walked`` is how many files the walk saw: nothing at all under a root with
    catalog rows is what a dropped mount looks like, so it refuses at any size.
    Otherwise ``max_prune_rows`` always applies, and ``max_prune_percent`` applies
    above ``prune_floor_rows``, so a small library can still lose a file or two.
    """

    hint = "Rows kept. If this is real, run `home-theater scan --force-prune`."
    if walked == 0:
        raise PruneRefusedError(
            f"root reads as empty but {stale} catalog row(s) sit under it. {hint}"
        )
    percent = stale * 100 / total
    over_rows = stale > limits.max_prune_rows
    over_percent = stale > limits.prune_floor_rows and percent > limits.max_prune_percent
    if over_rows or over_percent:
        raise PruneRefusedError(
            f"refusing to prune {stale} of {total} catalog rows ({percent:.1f}%): over the "
            f"limit of {limits.max_prune_percent:g}% or {limits.max_prune_rows} rows. {hint}"
        )


def prune_fingerprint(paths: Iterable[str]) -> str:
    """Identifies one exact set of rows, so accepting a refused prune can't turn into
    accepting whatever a later walk happens to miss."""

    digest = hashlib.sha256("\n".join(sorted(paths)).encode("utf-8", "surrogateescape"))
    return digest.hexdigest()[:32]


def _refusal(
    root: str, prefix: str, stale: list[str], *, total: int, walked: int, limits: Scanner
) -> dict[str, Any]:
    """What a person needs to judge a refused prune. Paths are relative to the root:
    this lands in job_run, which the Runs page and API show to the whole LAN."""

    return {
        "root": root,
        "stale": len(stale),
        "total": total,
        "walked": walked,
        "percent": round(len(stale) * 100 / total, 1),
        "max_percent": limits.max_prune_percent,
        "max_rows": limits.max_prune_rows,
        "sample": [_safe_text(p.removeprefix(prefix)) for p in sorted(stale)[:_PRUNE_SAMPLE]],
        "fingerprint": prune_fingerprint(stale),
    }


def _prune_missing(
    start: str,
    seen_paths: set[str],
    stats: ScanStats,
    *,
    walk_started: datetime,
    limits: Scanner,
    force: bool = False,
    root: str | None = None,
    accepted: Mapping[str, str] | None = None,
) -> None:
    """Delete owned_file rows under ``start`` whose file was not seen this walk.

    The caller must only get here after a walk that raised nothing: both
    filesystems raise on any unreadable directory. That alone doesn't prove the
    files are gone, so a prune that fails :func:`_check_prune_allowed` raises and
    deletes nothing unless ``force`` is set. ``accepted`` narrows ``force`` to the
    exact reviewed set: root -> :func:`prune_fingerprint`. Any other root, or a walk
    that now misses different files, goes through the usual check.

    Only rows created before ``walk_started`` are considered: a row added since
    (an import finishing mid-scan) describes a file this walk may have passed
    before it existed. Titles are kept even when their last file goes away — they
    hold metadata and candidate history. Referencing subtitle rows are unlinked
    first (FK has no ON DELETE clause).
    """

    sep = "\\" if "\\" in start else "/"
    prefix = start.rstrip(sep) + sep  # trailing sep: "Movies" must not match "Movies HD"
    with session_scope() as session:
        rows = session.execute(
            select(OwnedFile.id, OwnedFile.path).where(
                OwnedFile.path.startswith(prefix, autoescape=True),
                OwnedFile.created_at < walk_started,
            )
        ).all()
        stale_rows = [(row_id, path) for row_id, path in rows if path not in seen_paths]
        stale = [row_id for row_id, _ in stale_rows]
        stale_paths = [path for _, path in stale_rows]
        if accepted is not None:
            force = force and accepted.get(root or start) == prune_fingerprint(stale_paths)
        if stale and not force:
            try:
                _check_prune_allowed(
                    stale=len(stale), total=len(rows), walked=len(seen_paths), limits=limits
                )
            except PruneRefusedError:
                stats.prune_refused.append(
                    _refusal(
                        root or start,
                        prefix,
                        stale_paths,
                        total=len(rows),
                        walked=len(seen_paths),
                        limits=limits,
                    )
                )
                raise
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
    *,
    limits: Scanner,
    force_prune: bool = False,
    accepted_prunes: Mapping[str, str] | None = None,
) -> None:
    """Walk ``root`` fully, upsert each media file in its own transaction, then
    prune rows for files that disappeared. Raises if the walk itself fails or the
    prune is refused; per-file problems are recorded in ``stats.errors`` and
    skipped, and never count as the file being gone."""

    start = fs.resolve(root)
    sep = "\\" if "\\" in start else "/"
    walk_started = utcnow()
    entries = list(fs.walk(root))  # finish the (slow) walk before any DB write
    stats.files_scanned += len(entries)
    # Everything walked exists, whatever happens to it below: only "not walked"
    # means gone. A file we can't parse or store must keep its row.
    seen_paths = {entry.path for entry in entries}

    def _split_parent(path: str) -> tuple[str, str]:
        sep = "\\" if "\\" in path else "/"
        head, _, tail = path.rpartition(sep)
        return head, tail

    # Sidecars next to the media file, subtitles inside a Subs/ subfolder
    # (keyed by the *media* directory), and media counts per directory (a
    # standalone "Hebrew.srt" is only attributable when the folder holds
    # exactly one media file — the movie-folder layout).
    subs_by_parent: dict[str, list[str]] = defaultdict(list)
    subsdir_by_parent: dict[str, list[str]] = defaultdict(list)
    media_count: dict[str, int] = defaultdict(int)
    for entry in entries:
        if is_subtitle_file(entry.name):
            head, tail = _split_parent(entry.parent)
            if tail.lower() in SUBS_DIR_NAMES:
                subsdir_by_parent[head].append(entry.name)
            else:
                subs_by_parent[entry.parent].append(entry.name)
        elif is_media_file(entry.name):
            media_count[entry.parent] += 1

    def _langs_for(media_name: str, parent: str) -> list[str]:
        langs = {
            lang
            for sub in subs_by_parent.get(parent, [])
            if (lang := subtitle_lang_for(media_name, sub)) is not None
        }
        for sub in subsdir_by_parent.get(parent, []):
            lang = subtitle_lang_for(media_name, sub)
            if lang is None and media_count[parent] == 1:
                # Movie-folder Subs/: language-named files (2_English.srt).
                lang = subtitle_lang_standalone(sub)
            if lang is not None:
                langs.add(lang)
        return sorted(langs)

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
            # A TV file with no episode number is an extra/featurette shipped in a
            # season pack ("The Cast Ask Each Other Anything"). Cataloging it makes
            # a junk, poster-less title — skip it (mirrors the importer).
            if kind is TitleKind.series and parsed.episode is None:
                stats.files_skipped += 1
                log.info("scan.extra_skipped", path=entry.path)
                continue
            langs = _langs_for(entry.name, entry.parent)
            folder_prefix = (
                f"{start.rstrip(sep)}{sep}{parsed.show_folder}{sep}" if parsed.show_folder else None
            )
            with session_scope() as session:  # short per-file transaction
                added, created = _upsert_owned_file(
                    session,
                    entry.path,
                    entry.size,
                    parsed,
                    langs or None,
                    walk_started=walk_started,
                    folder_prefix=folder_prefix,
                )
            stats.titles_created += int(created)
            stats.files_added += int(added)
            stats.files_updated += int(not added)
            stats.subtitles_found += len(langs)
        except Exception as exc:  # keep scanning; record the bad file
            log.warning("scan.file_failed", path=entry.path, error=redact_exc(exc))
            stats.errors.append(f"{entry.path}: {redact_exc(exc)}")

    _prune_missing(
        start,
        seen_paths,
        stats,
        walk_started=walk_started,
        limits=limits,
        force=force_prune,
        root=root,
        accepted=accepted_prunes,
    )


def scan_library(
    fs: FileSystem,
    roots: Mapping[TitleKind, str],
    *,
    limits: Scanner | None = None,
    force_prune: bool = False,
    accepted_prunes: Mapping[str, str] | None = None,
) -> ScanStats:
    """Scan the given roots and upsert the catalog. Returns run statistics.

    ``roots`` maps a :class:`TitleKind` to the root path for that kind, e.g.
    ``{TitleKind.movie: "Movies", TitleKind.series: "TV Shows"}``. A root whose
    walk fails (e.g. SMB outage) is skipped without pruning and the run is
    marked failed (raising at the end), but other roots still complete and
    their results are kept. The same goes for a root whose prune is refused
    (see :func:`_prune_missing`); its new and updated files are still saved.
    ``force_prune`` lifts that refusal, never the failure of a walk; with
    ``accepted_prunes`` it lifts only the refusals someone reviewed (see
    :func:`_prune_missing`).

    Callers wanting the ``scan`` job lock should use :func:`homeTheater.scanner.run_scan`.
    """

    limits = limits or Scanner()

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
                _scan_root(
                    fs,
                    kind,
                    root,
                    stats,
                    limits=limits,
                    force_prune=force_prune,
                    accepted_prunes=accepted_prunes,
                )
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
