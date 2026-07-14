"""Torrent acquisition backend: search indexers, grab a magnet, track it.

This is the ``acquisition.backend == 'torrent'`` counterpart to the Radarr/Sonarr
path. It reuses the same ``Candidate``/``Download`` state machine and the
``QueueOutcome``/``SyncStats`` DTOs, so the CLI, scheduler, dashboard and
``dry_run`` gate behave identically — only the grab/poll mechanics differ.

Series support is intentionally basic: we grab a single best-effort season/complete
release as one download and don't track per-episode state (that's a larger
follow-up). Movies are the fully-supported path.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select as sa_select

from ...config import AppConfig
from ...db.base import utcnow
from ...db.models import (
    Candidate,
    CandidateStatus,
    Download,
    OwnedFile,
    Title,
    TitleKind,
)
from ...db.session import session_scope
from ...errors import InvalidTransitionError, NotConfiguredError, redact_exc
from ...logging_setup import get_logger
from ..service import QueueOutcome, SyncStats
from .base import DownloadClient, TorrentRelease, TorrentSource, TorrentStatus
from .select import build_query, select_release
from .sources import PirateBaySource, RarbgSource, X1337Source
from .transmission import TransmissionClient

log = get_logger(__name__)

_DEFAULT_TRANSMISSION_URL = "http://localhost:9091/transmission/rpc"


@dataclass(frozen=True, slots=True)
class _Snap:
    candidate_id: int
    title_id: int
    kind: TitleKind
    title: str
    year: int | None
    status: CandidateStatus
    has_download: bool
    season: int | None = None  # season-scoped grab (new season of an owned series)


def _load_snap(candidate_id: int) -> _Snap | None:
    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        if cand is None:
            return None
        title = s.get(Title, cand.title_id)
        if title is None:
            return None
        has_download = (
            s.scalar(sa_select(Download.id).where(Download.candidate_id == cand.id)) is not None
        )
        return _Snap(
            cand.id,
            title.id,
            title.kind,
            title.title,
            title.year,
            cand.status,
            has_download,
            cand.season,
        )


def _build_sources(config: AppConfig, http: httpx.AsyncClient) -> list[TorrentSource]:
    t = config.torrent
    sources: list[TorrentSource] = []
    for name in t.enabled_sources:
        if name == "piratebay":
            sources.append(PirateBaySource(t.piratebay_api_url, http, timeout=t.request_timeout))
        elif name == "1337x":
            sources.append(
                X1337Source(
                    t.x1337_base_url,
                    http,
                    flaresolverr_url=t.flaresolverr_url,
                    timeout=t.request_timeout,
                )
            )
        elif name == "rarbg":
            sources.append(
                RarbgSource(
                    t.rarbg_base_url,
                    http,
                    flaresolverr_url=t.flaresolverr_url,
                    timeout=t.request_timeout,
                )
            )
        else:
            log.warning("torrent.unknown_source", source=name)
    return sources


def _download_client(config: AppConfig, http: httpx.AsyncClient) -> DownloadClient:
    s = config.secrets
    return TransmissionClient(
        s.transmission_url or _DEFAULT_TRANSMISSION_URL,
        http,
        username=s.transmission_user,
        password=s.transmission_pass.get_secret_value() if s.transmission_pass else None,
        timeout=config.torrent.request_timeout,
    )


def _download_dir(config: AppConfig, kind: TitleKind) -> str | None:
    t = config.torrent
    return t.movie_download_dir if kind is TitleKind.movie else t.series_download_dir


def _mount_to_unc(dest: str, config: AppConfig) -> str:
    """Map a local mount dest back to the SMB UNC path the scanner uses, so a later
    NAS scan reconciles this file instead of pruning + re-adding it."""

    base = config.torrent.library_base_dir
    host = config.secrets.smb_host
    share = config.nas.share
    if base and host and share:
        b = base.rstrip("/")
        if dest.startswith(b):
            rel = dest[len(b) :].lstrip("/").replace("/", "\\")
            return f"\\\\{host}\\{share}\\{rel}"
    return dest


def register_owned_movie(config: AppConfig, title_id: int, dest: str, release_name: str) -> int:
    """Catalog a just-imported movie as an ``OwnedFile`` linked to its known Title.

    Parses the *release* name (which keeps quality tags the clean library filename
    drops) for resolution/codec. Idempotent on the UNC path. Returns the owned-file
    id so callers can fetch its subtitles immediately.
    """

    from ...scanner.parse import parse_media

    parsed = parse_media(release_name, kind_hint=TitleKind.movie)
    unc = _mount_to_unc(dest, config)
    try:
        size = os.path.getsize(dest)
    except OSError:
        size = None
    with session_scope() as s:
        owned = s.scalar(sa_select(OwnedFile).where(OwnedFile.path == unc))
        if owned is None:
            owned = OwnedFile(path=unc, title_id=title_id, kind=TitleKind.movie)
            s.add(owned)
        owned.title_id = title_id  # keep the enriched candidate Title, don't re-resolve
        owned.kind = TitleKind.movie
        if parsed is not None:
            owned.resolution = parsed.resolution
            owned.codec = parsed.codec
            owned.container = parsed.container
        owned.size_bytes = size
        s.flush()
        return owned.id


async def remove_torrents(config: AppConfig, hashes: list[str]) -> None:
    """Remove torrents (+ their local data) from the client; best-effort.

    Used by restart to clear a stuck/leftover grab. A hash the client no longer
    knows is a harmless no-op.
    """

    async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
        client = _download_client(config, http)
        for infohash in hashes:
            try:
                await client.remove(infohash, delete_data=True)
            except Exception as exc:  # missing torrent / transport hiccup
                log.warning("torrent.remove_failed", infohash=infohash, detail=redact_exc(exc))


async def _search_all(
    sources: list[TorrentSource], query: str, kind: TitleKind
) -> list[TorrentRelease]:
    """Search every source concurrently; a failing source degrades to no hits."""

    async def one(src: TorrentSource) -> list[TorrentRelease]:
        try:
            return await src.search(query, kind)
        except Exception as exc:  # a dead mirror must not sink the whole grab
            log.warning("torrent.source_failed", source=src.name, detail=redact_exc(exc))
            return []

    results = await asyncio.gather(*(one(s) for s in sources))
    return [rel for group in results for rel in group]


async def queue_candidate_torrent(config: AppConfig, candidate_id: int) -> QueueOutcome:
    """Search + grab one candidate (or log intent in dry-run). State-guarded like
    the arr path: new/approved/failed are queueable; queued/downloading/imported
    are no-ops; rejected is an error."""

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

    async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
        sources = _build_sources(config, http)
        if not sources:
            raise NotConfiguredError(
                "No torrent sources enabled; set torrent.enabled_sources in config.yaml."
            )
        query = build_query(snap.title, snap.year, snap.kind, season=snap.season)
        releases = await _search_all(sources, query, snap.kind)
        allowed = config.torrent.resolutions or config.thresholds.allowed_resolutions
        chosen = select_release(
            releases, allowed_resolutions=allowed, min_seeders=config.torrent.min_seeders
        )
        if chosen is None:
            return QueueOutcome(
                candidate_id,
                False,
                config.features.dry_run,
                None,
                f"no suitable release for '{query}' ({len(releases)} hits)",
            )

        if config.features.dry_run:
            log.info(
                "acquire.dry_run",
                candidate=candidate_id,
                title=snap.title,
                release=chosen.title,
                source=chosen.source,
                seeders=chosen.seeders,
            )
            return QueueOutcome(
                candidate_id,
                False,
                True,
                None,
                f"would grab '{chosen.title}' from {chosen.source} ({chosen.seeders} seeders)",
            )

        magnet = chosen.magnet_uri()
        assert magnet is not None  # select_release only returns releases with a magnet
        client = _download_client(config, http)
        added = await client.add_magnet(magnet, download_dir=_download_dir(config, snap.kind))

    with session_scope() as s:
        existing = s.scalar(
            sa_select(Download).where(
                Download.candidate_id == candidate_id,
                Download.external_id == added.infohash,
            )
        )
        if existing is None:
            s.add(
                Download(
                    candidate_id=candidate_id,
                    external_id=added.infohash,
                    release=chosen.title,
                    state="downloading",
                    progress=0.0,
                )
            )
        cand = s.get(Candidate, candidate_id)
        if cand is not None:
            cand.status = CandidateStatus.queued
            if cand.decided_at is None:
                cand.decided_at = utcnow()
    message = "grabbed (already in client)" if added.already_existed else "grabbed"
    return QueueOutcome(candidate_id, True, False, None, message)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def sync_downloads_torrent(config: AppConfig) -> SyncStats:
    """Poll Transmission for in-flight torrents, import completed ones, advance state.

    On completion a movie is copied into the NAS library (see :mod:`.importer`);
    the candidate only reaches ``imported`` once that copy succeeds, so a failed
    import leaves the download in ``completed`` to be retried next sweep. Mirrors
    the arr sync otherwise: never resurrects a rejected candidate, and a torrent
    the client no longer knows (or that stalls) eventually flips to ``failed``.
    """

    stale_after = timedelta(hours=config.torrent.stale_after_hours)
    with session_scope() as s:
        # Include "completed": a torrent that finished but whose import failed sits
        # there awaiting a retry.
        rows: list[tuple[int, str]] = []
        meta: dict[int, tuple[int, TitleKind, str, int | None]] = {}
        for dl, title in s.execute(
            sa_select(Download, Title)
            .join(Candidate, Candidate.id == Download.candidate_id)
            .join(Title, Title.id == Candidate.title_id)
            .where(Download.state.in_(("queued", "downloading", "importing", "completed")))
        ).all():
            if not dl.external_id:
                continue
            rows.append((dl.id, dl.external_id))
            meta[dl.id] = (title.id, title.kind, title.title, title.year)

    stats = SyncStats()
    # A large NAS copy that fails (e.g. the SMB mount drops mid-transfer) tends to
    # break the mount for the rest of the run; don't cascade — stop importing after
    # the first failure and let the others retry next sweep.
    imports_blocked = False
    async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
        client = _download_client(config, http)
        for download_id, infohash in rows:
            stats.checked += 1
            try:
                st = await client.status(infohash)
            except Exception as exc:
                stats.errors.append(f"{infohash}: {redact_exc(exc)}")
                continue

            if st is not None and st.complete:
                if imports_blocked:
                    continue  # deferred to the next sweep after an earlier failure
                title_id, kind, title, year = meta[download_id]
                err = await _finish_completed(
                    config, client, stats, download_id, infohash, st, title_id, kind, title, year
                )
                if err is not None:
                    imports_blocked = True
                continue

            with session_scope() as s:
                dl = s.get(Download, download_id)
                if dl is None:
                    continue
                cand = s.get(Candidate, dl.candidate_id)
                past_grace = utcnow() - _aware(dl.created_at) > stale_after
                if cand is not None and cand.status is CandidateStatus.rejected:
                    dl.state = "cancelled"
                elif st is not None and st.downloading and not (st.progress <= 0.0 and past_grace):
                    dl.state = "downloading"
                    dl.progress = st.progress
                    dl.save_path = st.save_path
                    if cand is not None:
                        cand.status = CandidateStatus.downloading
                    stats.downloading += 1
                elif past_grace:
                    # Gone from the client, or "downloading" at 0% past the grace
                    # window (dead magnet / no seeders) — Transmission reports such a
                    # torrent as active forever, so time-box it here.
                    dl.state = "failed"
                    dl.error = "torrent not found in client, or stalled at 0% past the grace window"
                    if cand is not None:
                        cand.status = CandidateStatus.failed
                    stats.failed += 1
    return stats


def _set_download(download_id: int, **fields: Any) -> None:
    with session_scope() as s:
        dl = s.get(Download, download_id)
        if dl is not None:
            for key, value in fields.items():
                setattr(dl, key, value)


def _import_progress_cb(download_id: int) -> Callable[[int, int], None]:
    """A throttled callback that records copy progress onto the Download (~1% steps)."""

    last = [0.0]

    def cb(copied: int, total: int) -> None:
        frac = (copied / total) if total else 0.0
        if frac - last[0] >= 0.01 or frac >= 1.0:
            last[0] = frac
            _set_download(download_id, progress=round(frac, 3))

    return cb


async def _finish_completed(
    config: AppConfig,
    client: DownloadClient,
    stats: SyncStats,
    download_id: int,
    infohash: str,
    st: TorrentStatus,
    title_id: int,
    kind: TitleKind,
    title: str,
    year: int | None,
) -> str | None:
    """Import a finished torrent into the library and mark it imported.

    Movies are copied into the NAS layout; series are left in the download dir
    (per-episode placement isn't modelled yet) but still marked imported. A
    failed movie import records the error and leaves state ``completed`` so the
    next sync retries — the candidate is not advanced until the file is in place.
    Returns the error string on a failed import (else ``None``).
    """

    dest: str | None = None
    error: str | None = None

    if kind is TitleKind.movie and config.torrent.import_to_library:
        content = st.content_path()
        if content is None:
            error = "client reported no content path"
        else:
            try:
                from .importer import build_library_target, import_completed_movie

                target = build_library_target(config)
                # Mark 'importing' + copy in a worker thread so the big NAS copy
                # neither blocks the event loop nor hides its progress: the copy
                # updates Download.progress, which the Activity view polls live.
                _set_download(download_id, state="importing", progress=0.0)
                dest = await asyncio.to_thread(
                    import_completed_movie,
                    config,
                    target,
                    content_path=content,
                    title=title,
                    year=year,
                    on_progress=_import_progress_cb(download_id),
                )
            except Exception as exc:
                error = f"import failed: {redact_exc(exc)}"
                log.warning("import.failed", download=download_id, title=title, detail=error)
    elif kind is TitleKind.series:
        log.info("import.series_skipped", download=download_id, title=title)

    if error is not None:
        with session_scope() as s:
            dl = s.get(Download, download_id)
            if dl is not None:
                dl.state = "completed"  # torrent done; import pending retry
                dl.progress = 1.0
                dl.save_path = st.save_path
                dl.error = error
        stats.errors.append(f"{infohash}: {error}")
        return error

    if dest is not None and config.torrent.delete_local_after_import:
        try:
            await client.remove(infohash, delete_data=True)
        except Exception as exc:
            log.warning("import.cleanup_failed", download=download_id, detail=redact_exc(exc))

    with session_scope() as s:
        dl = s.get(Download, download_id)
        if dl is not None:
            dl.state = "imported"
            dl.progress = 1.0
            dl.save_path = dest or st.save_path
            dl.error = None
            dl.completed_at = utcnow()
            cand = s.get(Candidate, dl.candidate_id)
            if cand is not None and cand.status is not CandidateStatus.rejected:
                cand.status = CandidateStatus.imported
    stats.completed += 1

    # Complete the pipeline: catalog the file (so it's "owned") and fetch its
    # subtitles now, instead of waiting for a NAS rescan + a full sweep. Best-effort
    # — a failure here never un-imports the movie.
    if kind is TitleKind.movie and dest is not None:
        try:
            owned_id = register_owned_movie(config, title_id, dest, st.name or title)
            if config.subtitles.backend == "native":
                from ...subtitles.native.service import fetch_for_owned_file

                got = await fetch_for_owned_file(config, owned_id)
                log.info("import.subtitles", download=download_id, title=title, fetched=len(got))
        except Exception as exc:
            log.warning("import.catalog_failed", download=download_id, detail=redact_exc(exc))
    return None
