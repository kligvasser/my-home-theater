"""Import reconciliation (plan §5.7).

Radarr/Sonarr own the rename/import/move on the NAS; this module only reflects a
completed import back into our catalog. Two entry points:

* :func:`reconcile_import` — apply one normalized import event (webhook-driven).
* :func:`reconcile_library` — poll Radarr/Sonarr and reconcile the whole library
  (catches imports we didn't originate; plan §5.9). Records a ``reconcile`` run.

Both are idempotent: title identity is a (kind-scoped) TMDb/TVDB/IMDb id,
owned-file identity is the path, and a candidate already ``imported`` stays
imported. ``reconcile_library`` also maintains ``Title.arr_has_file`` so a title
Radarr/Sonarr has on disk counts as owned for discovery even before the NAS
scanner sees the file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import (
    Candidate,
    CandidateStatus,
    Download,
    JobRun,
    OwnedFile,
    RunStatus,
    Title,
    TitleKind,
)
from ..db.session import session_scope
from ..errors import redact_exc
from ..logging_setup import bind_run, clear_run, get_logger
from .events import ImportEvent

log = get_logger(__name__)

LIVE_STATUSES = (
    CandidateStatus.new,
    CandidateStatus.approved,
    CandidateStatus.queued,
    CandidateStatus.downloading,
)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    title_id: int
    file_created: bool
    candidate_imported: bool


@dataclass
class ReconcileStats:
    checked: int = 0
    titles_created: int = 0
    files_created: int = 0
    imported: int = 0
    arr_flag_set: int = 0
    arr_flag_cleared: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _find_title(
    session: Session,
    *,
    kind: TitleKind,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
) -> Title | None:
    """Deterministic, kind-scoped lookup: tmdb, then tvdb, then imdb.

    Kind-scoped because TMDb movie/TV ids are independent sequences; an OR-match
    across ids could grab (and previously corrupted) a same-id title of the
    other kind.
    """

    if tmdb_id is not None:
        title = session.scalar(select(Title).where(Title.tmdb_id == tmdb_id, Title.kind == kind))
        if title is not None:
            return title
    if tvdb_id is not None:
        title = session.scalar(select(Title).where(Title.tvdb_id == tvdb_id, Title.kind == kind))
        if title is not None:
            return title
    if imdb_id:
        return session.scalar(select(Title).where(Title.imdb_id == imdb_id, Title.kind == kind))
    return None


def _backfill_ids(session: Session, title: Title, event: ImportEvent) -> None:
    """Fill missing ids, skipping (with a log) any that would collide with
    another row's unique index — a webhook must never 500 on an IntegrityError."""

    if title.tmdb_id is None and event.tmdb_id is not None:
        conflict = session.scalar(
            select(Title.id).where(
                Title.tmdb_id == event.tmdb_id, Title.kind == title.kind, Title.id != title.id
            )
        )
        if conflict is None:
            title.tmdb_id = event.tmdb_id
        else:
            log.warning("reconcile.tmdb_id_conflict", title=title.title, other=conflict)
    if title.tvdb_id is None and event.tvdb_id is not None:
        title.tvdb_id = event.tvdb_id
    if title.imdb_id is None and event.imdb_id:
        conflict = session.scalar(
            select(Title.id).where(Title.imdb_id == event.imdb_id, Title.id != title.id)
        )
        if conflict is None:
            title.imdb_id = event.imdb_id
        else:
            log.warning("reconcile.imdb_id_conflict", title=title.title, other=conflict)


def _mark_candidate_imported(session: Session, title_id: int) -> bool:
    cand = session.scalar(
        select(Candidate)
        .where(Candidate.title_id == title_id, Candidate.status.in_(LIVE_STATUSES))
        .order_by(Candidate.id.desc())
    )
    if cand is None:
        return False
    cand.status = CandidateStatus.imported
    cand.decided_at = utcnow()
    for dl in session.scalars(select(Download).where(Download.candidate_id == cand.id)):
        if dl.state != "completed":
            dl.state = "completed"
            dl.completed_at = utcnow()
    return True


def reconcile_import(event: ImportEvent) -> ReconcileResult:
    """Apply one import event to the catalog. Idempotent."""

    with session_scope() as session:
        title = _find_title(
            session,
            kind=event.kind,
            tmdb_id=event.tmdb_id,
            tvdb_id=event.tvdb_id,
            imdb_id=event.imdb_id,
        )
        if title is None:
            title = Title(kind=event.kind, title=event.title)
            session.add(title)
        title.title = event.title or title.title
        title.year = title.year or event.year
        _backfill_ids(session, title, event)
        title.arr_has_file = True
        session.flush()

        file_created = False
        if event.path:
            owned = session.scalar(select(OwnedFile).where(OwnedFile.path == event.path))
            if owned is None:
                owned = OwnedFile(path=event.path, title_id=title.id, kind=event.kind)
                session.add(owned)
                file_created = True
            owned.title_id = title.id
            owned.kind = event.kind
            owned.season = event.season
            owned.episode = event.episode
            owned.episode_end = event.episode_end
            owned.resolution = event.resolution or owned.resolution
            owned.size_bytes = event.size_bytes or owned.size_bytes

        imported = _mark_candidate_imported(session, title.id)
        log.info(
            "reconcile.import",
            title=title.title,
            title_id=title.id,
            file_created=file_created,
            candidate_imported=imported,
        )
        return ReconcileResult(title.id, file_created, imported)


async def reconcile_library(config: AppConfig) -> ReconcileStats:
    """Poll Radarr/Sonarr and reconcile owned items into the catalog."""

    from ..acquisition.service import _radarr, _sonarr

    with session_scope() as s:
        run = JobRun(kind="reconcile", started_at=utcnow(), status=RunStatus.running)
        s.add(run)
        s.flush()
        run_id = run.id

    bind_run(run_id=run_id, job="reconcile")
    stats = ReconcileStats()
    status = RunStatus.success
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            clients = [c for c in (_radarr(config, http), _sonarr(config, http)) if c is not None]
            for client in clients:
                kind = client.kind
                seen_title_ids: set[int] = set()
                for ref in await client.list_owned():
                    stats.checked += 1
                    if not ref.has_file:
                        continue
                    with session_scope() as session:
                        title = _find_title(
                            session,
                            kind=kind,
                            tmdb_id=ref.tmdb_id,
                            tvdb_id=ref.tvdb_id,
                            imdb_id=None,
                        )
                        if title is None:
                            title = Title(
                                kind=kind,
                                title=ref.title,
                                tmdb_id=ref.tmdb_id,
                                tvdb_id=ref.tvdb_id,
                            )
                            session.add(title)
                            session.flush()
                            stats.titles_created += 1
                        if not title.arr_has_file:
                            title.arr_has_file = True
                            stats.arr_flag_set += 1
                        seen_title_ids.add(title.id)
                        if ref.path:
                            owned = session.scalar(
                                select(OwnedFile).where(OwnedFile.path == ref.path)
                            )
                            if owned is None:
                                session.add(OwnedFile(path=ref.path, title_id=title.id, kind=kind))
                                stats.files_created += 1
                        if _mark_candidate_imported(session, title.id):
                            stats.imported += 1

                # Items deleted from the arr no longer count as arr-owned.
                with session_scope() as session:
                    stale_q = select(Title).where(Title.kind == kind, Title.arr_has_file.is_(True))
                    if seen_title_ids:
                        stale_q = stale_q.where(Title.id.not_in(seen_title_ids))
                    for t in session.scalars(stale_q).all():
                        t.arr_has_file = False
                        stats.arr_flag_cleared += 1
        log.info("reconcile.done", **stats.as_dict())
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(redact_exc(exc))
        log.error("reconcile.failed", error=redact_exc(exc))
    finally:
        with session_scope() as s:
            job_run = s.get(JobRun, run_id)
            if job_run is not None:
                job_run.finished_at = utcnow()
                job_run.status = status
                job_run.stats = stats.as_dict()
        clear_run()

    if status is RunStatus.failed:
        raise RuntimeError(f"Reconcile failed: {stats.errors}")
    return stats
