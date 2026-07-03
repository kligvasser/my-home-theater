"""Acquisition service (plan §5.6): hand approved candidates to Radarr/Sonarr.

Everything that can *grab* is gated behind ``features.dry_run``: in dry-run we log
the intended add and change nothing. Radarr/Sonarr own the download client, import,
and renaming; we only add + monitor + track state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import (
    Candidate,
    CandidateStatus,
    Download,
    JobRun,
    RunStatus,
    Title,
    TitleKind,
)
from ..db.session import session_scope
from ..errors import InvalidTransitionError, NotConfiguredError, redact_exc
from ..logging_setup import bind_run, clear_run, get_logger
from ..metadata.tmdb import TMDbClient
from .arr import RadarrClient, SonarrClient
from .base import LibraryAutomation

log = get_logger(__name__)

# A download that is neither in the arr queue nor produced a file for this long
# is considered failed (grab dropped, indexer dead, removed by hand in the arr).
STALE_DOWNLOAD_AFTER = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class QueueOutcome:
    candidate_id: int
    queued: bool
    dry_run: bool
    external_id: int | None
    message: str


@dataclass
class AcquireStats:
    considered: int = 0
    queued: int = 0
    dry_run: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SyncStats:
    checked: int = 0
    downloading: int = 0
    completed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Snap:
    candidate_id: int
    title_id: int
    kind: TitleKind
    tmdb_id: int | None
    tvdb_id: int | None
    title: str
    status: CandidateStatus
    has_download: bool


def _radarr(config: AppConfig, http: httpx.AsyncClient) -> RadarrClient | None:
    s = config.secrets
    if s.radarr_url and s.radarr_api_key:
        return RadarrClient(s.radarr_url, s.radarr_api_key.get_secret_value(), http)
    return None


def _sonarr(config: AppConfig, http: httpx.AsyncClient) -> SonarrClient | None:
    s = config.secrets
    if s.sonarr_url and s.sonarr_api_key:
        return SonarrClient(s.sonarr_url, s.sonarr_api_key.get_secret_value(), http)
    return None


def _client_for(
    kind: TitleKind, config: AppConfig, http: httpx.AsyncClient
) -> LibraryAutomation | None:
    return _radarr(config, http) if kind is TitleKind.movie else _sonarr(config, http)


def _load_snap(candidate_id: int) -> _Snap | None:
    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        if cand is None:
            return None
        title = s.get(Title, cand.title_id)
        if title is None:
            return None
        has_download = (
            s.scalar(select(Download.id).where(Download.candidate_id == cand.id)) is not None
        )
        return _Snap(
            cand.id,
            title.id,
            title.kind,
            title.tmdb_id,
            title.tvdb_id,
            title.title,
            cand.status,
            has_download,
        )


async def _resolve_external_id(
    config: AppConfig, http: httpx.AsyncClient, snap: _Snap
) -> int | None:
    """TMDb id for Radarr; TVDB id for Sonarr (resolved via TMDb + persisted)."""

    if snap.kind is TitleKind.movie:
        return snap.tmdb_id
    if snap.tvdb_id is not None:
        return snap.tvdb_id
    if snap.tmdb_id is None or config.secrets.tmdb_api_key is None:
        return None
    tmdb = TMDbClient(
        config.secrets.tmdb_api_key.get_secret_value(),
        http,
        language=config.metadata.language,
        cache_days=config.metadata.cache_days,
    )
    details = await tmdb.details(snap.tmdb_id, TitleKind.series)
    if details.tvdb_id is not None:
        with session_scope() as s:
            title = s.get(Title, snap.title_id)
            if title is not None:
                title.tvdb_id = details.tvdb_id
    return details.tvdb_id


def _profile_and_root(config: AppConfig, kind: TitleKind) -> tuple[str, str | None]:
    acq = config.acquisition
    if kind is TitleKind.movie:
        return acq.movie_quality_profile, acq.movie_root_folder
    return acq.series_quality_profile, acq.series_root_folder


async def queue_candidate(config: AppConfig, candidate_id: int) -> QueueOutcome:
    """Add one candidate to Radarr/Sonarr (or log intent in dry-run).

    Idempotent and state-guarded: queueing implies approval (new/approved/failed
    are queueable); an already-queued/downloading/imported candidate is a no-op;
    a rejected candidate is an error.
    """

    snap = _load_snap(candidate_id)
    if snap is None:
        raise ValueError(f"candidate {candidate_id} not found")

    if snap.status in (
        CandidateStatus.queued,
        CandidateStatus.downloading,
        CandidateStatus.imported,
    ) or (snap.has_download and snap.status is not CandidateStatus.failed):
        return QueueOutcome(
            candidate_id, False, config.features.dry_run, None, f"already {snap.status.value}"
        )
    if snap.status is CandidateStatus.rejected:
        raise InvalidTransitionError(
            f"candidate {candidate_id} was rejected; approve it again before queueing"
        )

    async with httpx.AsyncClient(timeout=20.0) as http:
        client = _client_for(snap.kind, config, http)
        if client is None:
            arr = "Radarr" if snap.kind is TitleKind.movie else "Sonarr"
            raise NotConfiguredError(f"{arr} is not configured in .env for {snap.kind.value}s.")

        external_id = await _resolve_external_id(config, http, snap)
        if external_id is None:
            return QueueOutcome(
                candidate_id, False, config.features.dry_run, None, "missing tmdb/tvdb id"
            )

        profile, root = _profile_and_root(config, snap.kind)
        if config.features.dry_run:
            log.info(
                "acquire.dry_run",
                candidate=candidate_id,
                title=snap.title,
                external_id=external_id,
                profile=profile,
            )
            return QueueOutcome(
                candidate_id, False, True, external_id, f"would add '{snap.title}' ({profile})"
            )

        result = await client.add(
            external_id,
            quality_profile=profile,
            root_folder=root,
            search=config.acquisition.search_on_add,
        )

    with session_scope() as s:
        existing = s.scalar(
            select(Download).where(
                Download.candidate_id == candidate_id,
                Download.external_id == str(result.external_id),
            )
        )
        if existing is None:
            s.add(
                Download(
                    candidate_id=candidate_id,
                    external_id=str(result.external_id),
                    release=snap.title,
                    state="downloading" if config.acquisition.search_on_add else "queued",
                )
            )
        cand = s.get(Candidate, candidate_id)
        if cand is not None:
            cand.status = CandidateStatus.queued
            if cand.decided_at is None:
                cand.decided_at = utcnow()
    message = "queued (already in arr)" if result.already_existed else "queued"
    return QueueOutcome(candidate_id, True, False, result.external_id, message)


async def queue_approved(config: AppConfig) -> AcquireStats:
    """Queue every approved candidate. Records an ``acquire`` job_run."""

    with session_scope() as s:
        run = JobRun(kind="acquire", started_at=utcnow(), status=RunStatus.running)
        s.add(run)
        s.flush()
        run_id = run.id
        ids = list(
            s.scalars(
                select(Candidate.id).where(Candidate.status == CandidateStatus.approved)
            ).all()
        )

    bind_run(run_id=run_id, job="acquire")
    stats = AcquireStats(considered=len(ids))
    status = RunStatus.success
    try:
        for cid in ids:
            try:
                outcome = await queue_candidate(config, cid)
                if outcome.dry_run:
                    stats.dry_run += 1
                elif outcome.queued:
                    stats.queued += 1
                else:
                    stats.skipped += 1
                    stats.errors.append(f"{cid}: {outcome.message}")
            except Exception as exc:
                stats.skipped += 1
                stats.errors.append(f"{cid}: {redact_exc(exc)}")
        log.info("acquire.done", **stats.as_dict())
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(redact_exc(exc))
    finally:
        with session_scope() as s:
            job_run = s.get(JobRun, run_id)
            if job_run is not None:
                job_run.finished_at = utcnow()
                job_run.status = status
                job_run.stats = stats.as_dict()
        clear_run()
    return stats


def _aware(dt: datetime) -> datetime:
    """SQLite can hand back naive datetimes for tz-aware columns; assume UTC."""

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def sync_downloads(config: AppConfig) -> SyncStats:
    """Poll Radarr/Sonarr for in-flight downloads and advance their state.

    Never resurrects a rejected candidate, and downloads that vanished from the
    arr queue without producing a file eventually flip to ``failed`` instead of
    sitting in ``downloading`` forever.
    """

    with session_scope() as s:
        rows: list[tuple[int, str, TitleKind]] = []
        downloads = s.scalars(
            select(Download).where(Download.state.in_(("queued", "downloading")))
        ).all()
        for d in downloads:
            if not d.external_id:
                continue
            cand = s.get(Candidate, d.candidate_id)
            title = s.get(Title, cand.title_id) if cand is not None else None
            if title is not None:
                rows.append((d.id, d.external_id, title.kind))

    stats = SyncStats()
    async with httpx.AsyncClient(timeout=20.0) as http:
        for download_id, external_id, kind in rows:
            stats.checked += 1
            client = _client_for(kind, config, http)
            if client is None:
                continue
            try:
                st = await client.status(int(external_id))
            except Exception as exc:
                stats.errors.append(f"{external_id}: {redact_exc(exc)}")
                continue
            with session_scope() as s:
                dl = s.get(Download, download_id)
                if dl is None:
                    continue
                cand = s.get(Candidate, dl.candidate_id)
                if cand is not None and cand.status is CandidateStatus.rejected:
                    # The user rejected it after the grab; reflect that, don't
                    # silently flip the candidate back to imported.
                    dl.state = "cancelled"
                    continue
                if st.has_file:
                    dl.state = "completed"
                    dl.completed_at = utcnow()
                    if cand is not None:
                        cand.status = CandidateStatus.imported
                    stats.completed += 1
                elif st.downloading:
                    dl.state = "downloading"
                    if cand is not None:
                        cand.status = CandidateStatus.downloading
                    stats.downloading += 1
                elif utcnow() - _aware(dl.created_at) > STALE_DOWNLOAD_AFTER:
                    dl.state = "failed"
                    dl.error = "no file and no longer in the arr queue"
                    if cand is not None:
                        cand.status = CandidateStatus.failed
                    stats.failed += 1
    return stats
