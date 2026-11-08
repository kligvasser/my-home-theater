"""Repair series split by the old filename-beats-folder parser.

A show used to become several titles when its filenames disagreed:
``The.Killing/Season 01/bkd-tk.s01e06.mkv`` made a title called ``bkd-tk``. The
scanner reads the folder now, but a known path keeps its title link, so existing
splits stay until they're folded back here. Runs only on request
(``home-theater repair-titles``), and only ``--apply`` changes anything. The
dashboard's Repair page drives the same plan, plus re-matches and manual merges.

The show folder is the evidence. A title that also has files in another folder is
not simply a duplicate of this one, so only this folder's files leave it. Nothing
is deleted from the NAS; a merged title's candidates, rejections, follow flag and
subtitles move to the title that stays.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import AppConfig
from ..db.models import Candidate, OwnedFile, Subtitle, Title, TitleKind
from ..db.session import session_scope
from ..errors import redact_exc
from ..locks import job_lock
from ..logging_setup import get_logger
from ..metadata.dto import TmdbTitle
from ..metadata.names import names_match
from ..metadata.service import _FILLED_ON_MERGE, apply_tmdb_details, merge_titles
from .parse import series_identity

log = get_logger(__name__)

_SEPARATOR_RE = re.compile(r"[\\/]+")


@dataclass(frozen=True, slots=True)
class TitleRef:
    id: int
    title: str
    year: int | None
    tmdb_id: int | None
    files: int  # files of this title under the folder being repaired


@dataclass(frozen=True, slots=True)
class FolderRepair:
    folder: str
    title: str  # folder-derived name
    year: int | None
    keep: TitleRef
    merge: tuple[TitleRef, ...] = ()  # folded into keep, row deleted
    move_files: tuple[TitleRef, ...] = ()  # also has files elsewhere: row stays
    rename_to: str | None = None  # keep has no TMDb identity to protect


@dataclass
class RepairPlan:
    repairs: list[FolderRepair] = field(default_factory=list)
    # Enriched titles whose name doesn't resemble their folder: maybe a bad TMDb
    # match, so they're shown, never changed.
    suspect: list[TitleRef] = field(default_factory=list)
    skipped_paths: int = 0  # series files not below the configured root


def _show_folder(path: str, root_parts: list[str]) -> str | None:
    """The folder directly under the series root, from a path on any mount."""

    parts = [p for p in _SEPARATOR_RE.split(path) if p]
    width = len(root_parts)
    folded = [p.casefold() for p in parts]
    for i in range(len(parts) - width - 1):
        if folded[i : i + width] == root_parts:
            return parts[i + width]
    return None


def _preference(ref: TitleRef, title: str) -> tuple[bool, bool, int, int]:
    return (names_match(title, [ref.title]), ref.tmdb_id is not None, ref.files, -ref.id)


def _plan_folder(
    folder: str, refs: list[TitleRef], other_folders: dict[int, bool], plan: RepairPlan
) -> None:
    identity = series_identity(folder)
    if identity is None:
        return
    title, year = identity
    keep = max(refs, key=lambda ref: _preference(ref, title))
    named = names_match(title, [keep.title])
    rename_to = None if keep.tmdb_id is not None or named else title
    others = [ref for ref in refs if ref.id != keep.id]
    if not named:
        # Nothing here carries the folder's name, so two enriched rows can't be told
        # apart from two shows. A person decides; unenriched rows hold nothing to lose.
        unclear = [ref for ref in others if ref.tmdb_id is not None]
        plan.suspect.extend(unclear)
        others = [ref for ref in others if ref.tmdb_id is None]
        if not others and rename_to is None:
            plan.suspect.append(keep)
    if not others and rename_to is None:
        return
    plan.repairs.append(
        FolderRepair(
            folder=folder,
            title=title,
            year=year,
            keep=keep,
            merge=tuple(ref for ref in others if not other_folders[ref.id]),
            move_files=tuple(ref for ref in others if other_folders[ref.id]),
            rename_to=rename_to,
        )
    )


def plan_title_repair(session: Session, tv_root: str) -> RepairPlan:
    """Work out the repair from the catalog alone. Reads, never writes."""

    root_parts = [p.casefold() for p in _SEPARATOR_RE.split(tv_root) if p]
    plan = RepairPlan()
    by_folder: dict[str, Counter[int]] = defaultdict(Counter)
    for path, title_id in session.execute(
        select(OwnedFile.path, OwnedFile.title_id).where(OwnedFile.kind == TitleKind.series)
    ):
        folder = _show_folder(path, root_parts)
        if folder is None:
            plan.skipped_paths += 1
        else:
            by_folder[folder][title_id] += 1

    # Titles whose files are all in one place can be merged whole.
    folders_of: Counter[int] = Counter()
    for counts in by_folder.values():
        folders_of.update(counts.keys())

    titles = {t.id: t for t in session.scalars(select(Title).where(Title.id.in_(folders_of)))}
    for folder, counts in sorted(by_folder.items()):
        refs = [
            TitleRef(tid, titles[tid].title, titles[tid].year, titles[tid].tmdb_id, files)
            for tid, files in counts.items()
        ]
        spread = {ref.id: folders_of[ref.id] > 1 for ref in refs}
        _plan_folder(folder, refs, spread, plan)
    return plan


def _move_folder_files(
    session: Session, source_id: int, target_id: int, folder: str, tv_root: str
) -> None:
    """Re-point only this folder's files (and their subtitles) to the target."""

    root_parts = [p.casefold() for p in _SEPARATOR_RE.split(tv_root) if p]
    ids = [
        file_id
        for file_id, path in session.execute(
            select(OwnedFile.id, OwnedFile.path).where(OwnedFile.title_id == source_id)
        )
        if _show_folder(path, root_parts) == folder
    ]
    session.execute(update(OwnedFile).where(OwnedFile.id.in_(ids)).values(title_id=target_id))
    session.execute(
        update(Subtitle).where(Subtitle.owned_file_id.in_(ids)).values(title_id=target_id)
    )


def _apply_folder(repair: FolderRepair, tv_root: str) -> None:
    with session_scope() as session:
        keep = session.get(Title, repair.keep.id)
        if keep is None:
            return
        for ref in repair.merge:
            dup = session.get(Title, ref.id)
            if dup is not None:
                # A name like "bkd-tk" may have been matched to any show at all, so
                # only a same-named duplicate's metadata is worth keeping.
                same_work = names_match(repair.title, [ref.title])
                merge_titles(session, dup=dup, canonical=keep, fill_metadata=same_work)
        for ref in repair.move_files:
            _move_folder_files(session, ref.id, keep.id, repair.folder, tv_root)
        if repair.rename_to is not None:
            keep.title = repair.rename_to
            keep.year = repair.year or keep.year
            keep.last_enriched_at = None  # now worth asking TMDb again


def repair_titles(tv_root: str, *, apply: bool = False) -> RepairPlan:
    """Plan the repair and, with ``apply``, carry it out. Returns the plan.

    Each folder commits alone, so a failure leaves earlier repairs done and a
    re-run finishes the rest.
    """

    with session_scope() as session:
        plan = plan_title_repair(session, tv_root)
    if not apply:
        return plan
    for repair in plan.repairs:
        _apply_folder(repair, tv_root)
        log.info(
            "repair.folder_done",
            folder=repair.folder,
            keep=repair.keep.id,
            merged=[ref.id for ref in repair.merge],
            moved=[ref.id for ref in repair.move_files],
        )
    return plan


# --- the dashboard's repair: previews a person reviews, applied only if unchanged ---


class ReviewOutdatedError(RuntimeError):
    """The catalog no longer matches what was reviewed, or the request can't be done."""


class TitleNotFoundError(LookupError):
    """A title named in a repair request doesn't exist."""


@contextlib.contextmanager
def repair_guard(config: AppConfig) -> Iterator[None]:
    """What every catalog repair runs under, CLI or dashboard: no scan or enrichment
    alongside it, and a backup first. JobBusyError if either is running."""

    from ..backup import backup_database

    with job_lock(config, "scan"), job_lock(config, "enrich"):
        backup_database(config)
        yield


def _token(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:32]


def repair_token(repair: FolderRepair) -> str:
    """Identifies one exact repair: its titles, what each would lose, the rename."""

    return _token(asdict(repair))


def plan_payload(plan: RepairPlan) -> dict[str, Any]:
    """The plan as the page reviews it. Each repair carries the token apply wants back."""

    return {
        "repairs": [{**asdict(r), "token": repair_token(r)} for r in plan.repairs],
        "suspect": [asdict(ref) for ref in plan.suspect],
        "skipped_paths": plan.skipped_paths,
    }


def preview_plan(tv_root: str) -> dict[str, Any]:
    with session_scope() as session:
        return plan_payload(plan_title_repair(session, tv_root))


@dataclass(slots=True)
class ApplyOutcome:
    applied: list[str] = field(default_factory=list)
    outdated: list[str] = field(default_factory=list)  # changed since review; left alone
    errors: list[str] = field(default_factory=list)


def apply_reviewed(config: AppConfig, reviewed: Mapping[str, str]) -> ApplyOutcome:
    """Carry out the reviewed repairs (folder -> token) and nothing else.

    The catalog is read again under the locks, and a repair runs only if it is still
    the one reviewed: a new file, a title gone, a changed rename all change its token.
    Such a folder is reported, not re-planned, and nothing else in the plan is touched.
    """

    outcome = ApplyOutcome()
    tv_root = config.nas.tv_root
    with repair_guard(config):
        with session_scope() as session:
            current = {r.folder: r for r in plan_title_repair(session, tv_root).repairs}
        for folder, token in reviewed.items():
            repair = current.get(folder)
            if repair is None or repair_token(repair) != token:
                outcome.outdated.append(folder)
                continue
            try:
                _apply_folder(repair, tv_root)
            except Exception as exc:  # each folder commits alone; carry on with the rest
                log.warning("repair.folder_failed", folder=folder, error=redact_exc(exc))
                outcome.errors.append(f"{folder}: {redact_exc(exc)}")
                continue
            outcome.applied.append(folder)
            log.info("repair.folder_done", folder=folder, keep=repair.keep.id)
    return outcome


def unmatched_titles(session: Session, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Titles with files but no TMDb identity, and how many there are in all."""

    owned = select(OwnedFile.title_id)
    where = (Title.tmdb_id.is_(None), Title.id.in_(owned))
    total = session.scalar(select(func.count()).select_from(Title).where(*where)) or 0
    rows = session.scalars(select(Title).where(*where).order_by(Title.id.desc()).limit(limit))
    return [
        {"id": t.id, "title": t.title, "year": t.year, "kind": t.kind.value} for t in rows
    ], total


def _title_or_raise(session: Session, title_id: int) -> Title:
    title = session.get(Title, title_id)
    if title is None:
        raise TitleNotFoundError(f"title {title_id} not found")
    return title


def _side(session: Session, title: Title) -> dict[str, Any]:
    """One title as a review shows it: what's at stake if it changes."""

    def count(model: Any) -> int:
        query = select(func.count()).select_from(model).where(model.title_id == title.id)
        return session.scalar(query) or 0

    statuses = session.execute(
        select(Candidate.status, func.count())
        .where(Candidate.title_id == title.id)
        .group_by(Candidate.status)
    ).all()
    return {
        "id": title.id,
        "title": title.title,
        "year": title.year,
        "kind": title.kind.value,
        "tmdb_id": title.tmdb_id,
        "followed": bool(title.followed),
        "files": count(OwnedFile),
        "subtitles": count(Subtitle),
        "candidates": {str(status): n for status, n in sorted(statuses)},
    }


def _merge_view(session: Session, dup_id: int, keep_id: int) -> tuple[Title, Title, dict[str, Any]]:
    if dup_id == keep_id:
        raise ReviewOutdatedError("pick two different titles")
    dup, keep = _title_or_raise(session, dup_id), _title_or_raise(session, keep_id)
    if dup.kind is not keep.kind:
        # tmdb ids are per kind, and a movie's rows mean nothing on a series.
        raise ReviewOutdatedError("a movie and a series can't be merged")
    view = {"dup": _side(session, dup), "keep": _side(session, keep)}
    return dup, keep, {**view, "token": _token(view)}


def merge_preview(dup_id: int, keep_id: int) -> dict[str, Any]:
    with session_scope() as session:
        return _merge_view(session, dup_id, keep_id)[2]


def apply_merge(config: AppConfig, dup_id: int, keep_id: int, token: str) -> dict[str, Any]:
    """Fold ``dup`` into ``keep``, exactly as reviewed. Children move and flags OR in
    :func:`merge_titles`; two different TMDb identities mean ``dup``'s metadata is
    about another work, so it's dropped rather than used to fill gaps."""

    with repair_guard(config), session_scope() as session:
        dup, keep, view = _merge_view(session, dup_id, keep_id)
        if view["token"] != token:
            raise ReviewOutdatedError("these titles changed since the preview; review again")
        different = bool(dup.tmdb_id and keep.tmdb_id and dup.tmdb_id != keep.tmdb_id)
        merge_titles(session, dup=dup, canonical=keep, fill_metadata=not different)
        log.info("repair.manual_merge", dup=dup_id, keep=keep_id)
        return view


def _rematch_view(
    session: Session, title: Title, details: TmdbTitle
) -> tuple[Title | None, dict[str, Any]]:
    if title.tmdb_id == details.tmdb_id:
        raise ReviewOutdatedError(f"title {title.id} is already TMDb {details.tmdb_id}")
    holder = session.scalar(  # tmdb ids are only unique within a kind
        select(Title).where(
            Title.tmdb_id == details.tmdb_id, Title.kind == title.kind, Title.id != title.id
        )
    )
    view: dict[str, Any] = {
        "title": _side(session, title),
        "match": {
            "tmdb_id": details.tmdb_id,
            "title": details.title,
            "year": details.year,
            "kind": title.kind.value,
        },
        "merges_into": _side(session, holder) if holder is not None else None,
    }
    return holder, {**view, "token": _token(view)}


def rematch_preview(title_id: int, details: TmdbTitle) -> dict[str, Any]:
    with session_scope() as session:
        return _rematch_view(session, _title_or_raise(session, title_id), details)[1]


def title_kind(title_id: int) -> TitleKind:
    with session_scope() as session:
        return _title_or_raise(session, title_id).kind


def _clear_identity(session: Session, title: Title) -> None:
    """Forget everything the wrong match supplied. apply_tmdb_details never overwrites
    with None and keeps an existing year, so a wrong value would otherwise outlive
    the re-match, and the old imdb_id would keep fetching the wrong rating."""

    for column in (*_FILLED_ON_MERGE, "imdb_id"):
        setattr(title, column, None)
    title.genres = []
    session.flush()


def apply_rematch(
    config: AppConfig, title_id: int, details: TmdbTitle, token: str
) -> dict[str, Any]:
    """Make ``title_id`` the work TMDb calls ``details``, exactly as reviewed.

    If another row already is that work, the title folds into it, leaving its wrong
    metadata behind. Enrichment then fetches ratings for the new identity.
    """

    with repair_guard(config), session_scope() as session:
        title = _title_or_raise(session, title_id)
        holder, view = _rematch_view(session, title, details)
        if view["token"] != token:
            raise ReviewOutdatedError("this title changed since the preview; review again")
        if holder is not None:
            merge_titles(session, dup=title, canonical=holder, fill_metadata=False)
            target = holder
        else:
            _clear_identity(session, title)
            title.tmdb_id = details.tmdb_id
            target = title
        apply_tmdb_details(session, target, details)
        target.last_enriched_at = None
        log.info("repair.rematched", title=title_id, tmdb_id=details.tmdb_id, into=target.id)
        return view
