"""Torrent acquisition backend: search indexers, grab a magnet, track it.

This is the ``acquisition.backend == 'torrent'`` counterpart to the Radarr/Sonarr
path. It reuses the same ``Candidate``/``Download`` state machine and the
``QueueOutcome``/``SyncStats`` DTOs, so the CLI, scheduler, dashboard and
``dry_run`` gate behave identically — only the grab/poll mechanics differ.

Series: a season-scoped candidate (``Candidate.season``) grabs that season's
pack when one exists, and falls back to grabbing the available episodes one
release each — a currently-airing season has no pack yet. Coverage is tracked
against the season's announced episode count (``features.season_episodes``)
plus whatever episodes are already on the NAS: when downloads finish with
episodes still missing, the candidate returns to ``approved`` so the next
acquire run tops it up. A title-level series candidate (approved from
trending/search) means "the whole series": it is expanded into one candidate
per aired season at grab time, each with that lifecycle. Movies are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...upgrades import UpgradePlan
    from .importer import EpisodeImport, ReplacementGrant

import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import AppConfig
from ...db.base import utcnow
from ...db.models import (
    LIVE_DOWNLOAD_STATES,
    MARKER_NEEDS_ATTENTION,
    MARKER_REMOVAL_PENDING,
    MARKER_REMOVED,
    MARKER_STALLED,
    STOPPED_STATUSES,
    Candidate,
    CandidateStatus,
    Download,
    DownloadTracking,
    GrabRetry,
    OwnedFile,
    Title,
    TitleKind,
)
from ...db.session import session_scope
from ...errors import InvalidTransitionError, NotConfiguredError, redact, redact_exc
from ...features import extract_features
from ...locks import import_active, is_job_running
from ...logging_setup import get_logger
from ..service import QueueOutcome, SyncStats, claim_candidate, release_claim
from .base import DownloadClient, TorrentRelease, TorrentSource, TorrentStatus
from .importer import LibraryConflictError, NasUnavailableError, mount_lost
from .select import (
    build_query,
    is_season_pack,
    parse_season_episode,
    season_words_query,
    select_release,
)
from .sources import PirateBaySource, RarbgSource, X1337Source
from .transmission import TransmissionClient

log = get_logger(__name__)

_DEFAULT_TRANSMISSION_URL = "http://localhost:9091/transmission/rpc"

# Marks an ``imported`` row whose torrent + local data still have to be removed.
# Written in the same commit as ``imported``, so a crash before the removal leaves
# a record the next sweep can finish. Must not start with "removed:": that's the
# quarantine ban marker.
_REMOVAL_PENDING = MARKER_REMOVAL_PENDING

# A failed import retries with growing gaps, then parks for a person. Only failures of
# this one item count: a NAS fault is the share's, and big files are expected to finish
# across several sweeps of those.
_IMPORT_MAX_ATTEMPTS = 5
_IMPORT_BACKOFF = timedelta(minutes=15)
_IMPORT_BACKOFF_MAX = timedelta(hours=6)

# A season whose last download died goes back to ``approved``, this many times, spaced
# out so a dead swarm isn't re-grabbed every cycle; after that it shows as ``failed``.
_GRAB_RETRY_MAX = 5
_GRAB_RETRY_BACKOFF = timedelta(hours=1)
_GRAB_RETRY_BACKOFF_MAX = timedelta(hours=24)

# Sweeps further apart than this mean nobody was watching (app down, host asleep), and
# time nobody watched can't count against a torrent: Transmission slept too.
_MIN_WATCH_GAP = timedelta(hours=1)

_THREAD_POLL_S = 0.2


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
        # Only an ACTIVE download blocks a re-grab; a failed/cancelled row (incl. a
        # quarantine ban) must not — else restarting a quarantined candidate is a
        # no-op ("already approved").
        has_download = (
            s.scalar(
                sa_select(Download.id).where(
                    Download.candidate_id == cand.id,
                    Download.state.in_(
                        ("queued", "downloading", "importing", "completed", "imported")
                    ),
                )
            )
            is not None
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


def _free_gb(path: str | None) -> float | None:
    """Free space (GB) on the filesystem holding ``path``, or None if unknown. A
    download dir that doesn't exist yet is measured at its nearest existing parent."""

    if not path:
        return None
    import shutil

    existing = path
    while not os.path.exists(existing) and existing != os.path.dirname(existing):
        existing = os.path.dirname(existing)
    try:
        return shutil.disk_usage(existing).free / 1e9
    except OSError:
        return None


def _disk_refusal(
    config: AppConfig, kind: TitleKind, in_flight_bytes: int = 0, release_bytes: int = 0
) -> str | None:
    """Why this grab must not start, or None. With no download dir configured the
    client picks one we can't see, so there's nothing to measure."""

    minimum = config.torrent.min_free_gb
    if not minimum:
        return None
    path = _download_dir(config, kind)
    free = _free_gb(path)
    if free is None:
        return f"can't read free space on {path!r}; not grabbing" if path else None
    after = free - (in_flight_bytes + release_bytes) / 1e9
    if after >= minimum:
        return None
    return (
        f"low local disk space: {free:.1f} GB free, {after:.1f} GB after this grab and "
        f"what's still downloading (< {minimum} GB) "
        "— free space or lower torrent.min_free_gb; not grabbing"
    )


async def _in_flight_bytes(client: DownloadClient) -> int:
    """Bytes the client still has to write. If it can't say, the add is about to find
    out for itself that the client is down."""

    try:
        torrents = await client.list_torrents()
    except Exception as exc:
        log.warning("torrent.in_flight_unknown", detail=redact_exc(exc))
        return 0
    return sum(t.left_bytes or 0 for t in torrents)


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


def register_owned_episode(config: AppConfig, title_id: int, ep: EpisodeImport) -> int:
    """Catalog a just-imported episode as an ``OwnedFile``, keyed by the UNC path
    the scanner uses. Idempotent."""

    from ...scanner.parse import parse_media

    parsed = parse_media(ep.filename, kind_hint=TitleKind.series)
    unc = _mount_to_unc(ep.dest, config)
    try:
        size = os.path.getsize(ep.dest)
    except OSError:
        size = None
    with session_scope() as s:
        owned = s.scalar(sa_select(OwnedFile).where(OwnedFile.path == unc))
        if owned is None:
            owned = OwnedFile(path=unc, title_id=title_id, kind=TitleKind.series)
            s.add(owned)
        owned.title_id = title_id  # keep the enriched candidate Title, don't re-resolve
        owned.kind = TitleKind.series
        owned.season = ep.season
        owned.episode = ep.episode
        owned.episode_end = ep.episode_end
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
            await _drop_torrent(client, infohash)


def _hash_in_use(infohash: str) -> bool:
    """Whether a live download (another candidate's, say) still needs this torrent."""

    with session_scope() as s:
        return (
            s.scalar(
                sa_select(Download.id).where(
                    Download.external_id == infohash, Download.state.in_(LIVE_DOWNLOAD_STATES)
                )
            )
            is not None
        )


async def _drop_torrent(client: DownloadClient, infohash: str) -> None:
    """Remove a torrent + its data, unless a live download still wants it. Call after
    the row saying so has committed."""

    if _hash_in_use(infohash):
        return
    try:
        await client.remove(infohash, delete_data=True)
    except Exception as exc:  # missing torrent / transport hiccup
        log.warning("torrent.remove_failed", infohash=infohash, detail=redact_exc(exc))


async def _search_all(
    config: AppConfig, sources: list[TorrentSource], query: str, kind: TitleKind
) -> list[TorrentRelease]:
    """Search every source concurrently; a failing source degrades to no hits. Each
    keeps its best-seeded ``torrent.max_results_per_source`` hits."""

    limit = config.torrent.max_results_per_source

    async def one(src: TorrentSource) -> list[TorrentRelease]:
        try:
            hits = await src.search(query, kind)
            return sorted(hits, key=lambda rel: rel.seeders, reverse=True)[:limit]
        except Exception as exc:  # a dead mirror must not sink the whole grab
            log.warning("torrent.source_failed", source=src.name, detail=redact_exc(exc))
            return []

    results = await asyncio.gather(*(one(s) for s in sources))
    return [rel for group in results for rel in group]


class _DiskRefusedError(RuntimeError):
    """The chosen release doesn't fit on the local disk; the message says why."""


async def queue_candidate_torrent(config: AppConfig, candidate_id: int) -> QueueOutcome:
    """Search + grab one candidate (or log intent in dry-run). State-guarded like
    the arr path: new/approved/failed are queueable; queued/downloading/imported
    are no-ops; rejected and cancelled are errors. A season candidate that's
    ``approved`` with downloads already recorded is an episode top-up, not a
    duplicate.

    A real grab starts with an atomic claim (the candidate goes ``queued``), so two
    runs can't both grab it and a cancel during the grab is seen when it's recorded.
    A grab that comes to nothing puts the status back."""

    snap = _load_snap(candidate_id)
    if snap is None:
        raise ValueError(f"candidate {candidate_id} not found")

    if snap.status in STOPPED_STATUSES:
        raise InvalidTransitionError(
            f"candidate {candidate_id} was {snap.status.value}; approve it again before queueing"
        )
    top_up = snap.season is not None and snap.status is CandidateStatus.approved
    if snap.status in (
        CandidateStatus.queued,
        CandidateStatus.downloading,
        CandidateStatus.imported,
    ) or (snap.has_download and snap.status is not CandidateStatus.failed and not top_up):
        return QueueOutcome(
            candidate_id, False, config.features.dry_run, None, f"already {snap.status.value}"
        )

    refusal = _disk_refusal(config, snap.kind)
    if refusal is not None:
        log.warning("acquire.low_disk", candidate=candidate_id, detail=refusal)
        return QueueOutcome(candidate_id, False, config.features.dry_run, None, refusal)

    if snap.kind is TitleKind.series and snap.season is None:
        # Approving a series means the whole series: one season candidate each. With
        # no season data there's no safe search: "Title" alone can match anything.
        outcome = await _queue_series(config, snap)
        if outcome is not None:
            return outcome
        return QueueOutcome(
            candidate_id,
            False,
            config.features.dry_run,
            None,
            f"no season data for '{snap.title}'; not grabbing a series blind",
        )

    if config.features.dry_run:
        return await _search_and_grab(config, snap)
    if not claim_candidate(candidate_id):
        return QueueOutcome(candidate_id, False, False, None, "already being handled elsewhere")
    try:
        return await _search_and_grab(config, snap)
    except _DiskRefusedError as exc:
        log.warning("acquire.low_disk", candidate=candidate_id, detail=str(exc))
        return QueueOutcome(candidate_id, False, False, None, str(exc))
    finally:
        release_claim(candidate_id, snap.status)


async def _search_and_grab(config: AppConfig, snap: _Snap) -> QueueOutcome:
    from ...upgrades import load_plan

    candidate_id = snap.candidate_id
    plan = load_plan(candidate_id)
    if plan is not None:
        return await _grab_upgrade(config, snap, plan)

    async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
        sources = _build_sources(config, http)
        if not sources:
            raise NotConfiguredError(
                "No torrent sources enabled; set torrent.enabled_sources in config.yaml."
            )
        allowed = config.torrent.resolutions or config.thresholds.allowed_resolutions

        if snap.kind is TitleKind.series and snap.season is not None:
            return await _queue_season(config, http, sources, snap, allowed)

        banned = _quarantined_hashes()
        dead = _dead_hashes()
        query = build_query(snap.title, snap.year, snap.kind)
        releases = await _search_all(config, sources, query, snap.kind)
        chosen = select_release(
            releases,
            allowed_resolutions=allowed,
            min_seeders=config.torrent.min_seeders,
            banned=banned,
            dead=dead,
            require_hd=config.torrent.require_hd,
            movie=True,
            expected_title=snap.title,
            expected_year=snap.year,
        )
        if chosen is None and snap.year:
            # TMDb's year can differ from the release year (e.g. a 2025 film TMDb
            # lists as 2026), so the year-pinned search finds only fakes. Retry
            # title-only; require_hd still guards the broader result set.
            alt = build_query(snap.title, None, snap.kind)
            if alt != query:
                releases = await _search_all(config, sources, alt, snap.kind)
                chosen = select_release(
                    releases,
                    allowed_resolutions=allowed,
                    min_seeders=config.torrent.min_seeders,
                    banned=banned,
                    dead=dead,
                    require_hd=config.torrent.require_hd,
                    movie=True,
                    expected_title=snap.title,
                    expected_year=snap.year,
                )
                query = alt
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

        client = _download_client(config, http)
        grabbed, dropped = await _grab_screened(client, config, snap, [chosen])

    if not grabbed:
        reason = dropped[0][2] if dropped else "no release grabbed"
        return QueueOutcome(candidate_id, False, False, None, f"dropped: {reason}; will retry")
    already = all(existed for _, _, existed in grabbed)
    message = "grabbed (already in client)" if already else "grabbed"
    return QueueOutcome(candidate_id, True, False, None, message)


async def _grab_upgrade(config: AppConfig, snap: _Snap, plan: UpgradePlan) -> QueueOutcome:
    """Grab the release an upgrade pinned: never a fresh search, so what downloads is
    what was proposed. A proposal whose file changed since is withdrawn."""

    from ...upgrades import blocked_reason, stale_reason, withdraw

    cid, dry_run = snap.candidate_id, config.features.dry_run
    release = plan.torrent_release()
    blocked = blocked_reason(config)
    if blocked is not None:
        return QueueOutcome(cid, False, dry_run, None, f"upgrade not grabbed: {blocked}")
    stale = stale_reason(plan)
    if stale is None and release.btih() in _quarantined_hashes():
        stale = "the release was quarantined since"
    if stale is not None:
        if not dry_run:
            withdraw(cid, stale)
        return QueueOutcome(cid, False, dry_run, None, f"upgrade withdrawn: {stale}")
    if dry_run:
        log.info("acquire.dry_run", candidate=cid, title=snap.title, release=release.title)
        return QueueOutcome(cid, False, True, None, f"would grab upgrade '{release.title}'")

    async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
        client = _download_client(config, http)
        grabbed, dropped = await _grab_screened(client, config, snap, [release])
    if not grabbed:
        reason = dropped[0][2] if dropped else "no release grabbed"
        return QueueOutcome(cid, False, False, None, f"dropped: {reason}")
    return QueueOutcome(cid, True, False, None, "grabbed upgrade")


# Right after a magnet is added, Transmission fetches the torrent's *metadata*
# (the file list) before any file content. We poll for that, then screen the
# names — so an executable ("clean listing, .exe inside") is removed while only
# metadata, not payload, has been transferred. Bounded; sync is the backstop if
# metadata hasn't resolved inside the window.
_METADATA_PROBE_ATTEMPTS = 15
_METADATA_PROBE_DELAY = 2.0


async def _grab(
    client: DownloadClient,
    config: AppConfig,
    candidate_id: int,
    kind: TitleKind,
    chosen: list[TorrentRelease],
) -> list[tuple[str, str, bool]]:
    """Add each release's magnet to the client → (infohash, release, existed).

    Each is recorded the moment it's added, so a failure part-way through leaves no
    torrent we've lost track of. Stops early if the candidate was stopped meanwhile.
    """

    out: list[tuple[str, str, bool]] = []
    for rel in chosen:
        magnet = rel.magnet_uri()
        assert magnet is not None  # select_release only returns releases with a magnet
        added = await client.add_magnet(
            magnet,
            download_dir=_download_dir(config, kind),
            seed_ratio_limit=config.torrent.seed_ratio_limit,
        )
        if not _record_added(candidate_id, added.infohash, rel.title):
            await _drop_torrent(client, added.infohash)
            break
        out.append((added.infohash, rel.title, added.already_existed))
    return out


async def _await_unsafe(client: DownloadClient, infohash: str) -> str | None:
    """Wait briefly for metadata, then judge the file list: reason if unsafe,
    None if clean or metadata didn't resolve in the window (sync backstops)."""

    for attempt in range(_METADATA_PROBE_ATTEMPTS):
        try:
            st = await client.status(infohash)
        except Exception as exc:
            # Screening is best-effort; sync re-scans the file list every sweep
            # and quarantines there too. A probe error must not abort the grab.
            log.warning("grab.screen_probe_failed", infohash=infohash, detail=redact_exc(exc))
            return None
        if st is None:
            return None  # gone from the client already
        if st.files is not None:
            return _unsafe_content(st.files)
        if attempt + 1 < _METADATA_PROBE_ATTEMPTS:
            await asyncio.sleep(_METADATA_PROBE_DELAY)
    return None


async def _screen_grabs(
    client: DownloadClient, grabbed: list[tuple[str, str, bool]]
) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str, str]]]:
    """Split fresh grabs into (safe, dropped) — dropping ones whose metadata
    reveals an executable/no-media, removing them from the client before they
    download real content. Dropped items are (infohash, release, reason)."""

    # Screen all grabs concurrently: each waits up to ~30s for metadata, so a
    # season top-up of 8-10 episodes must not block the acquire for minutes.
    reasons = await asyncio.gather(*(_await_unsafe(client, ih) for ih, _r, _e in grabbed))

    safe: list[tuple[str, str, bool]] = []
    dropped: list[tuple[str, str, str]] = []
    for (infohash, release, existed), reason in zip(grabbed, reasons, strict=True):
        if reason is None:
            safe.append((infohash, release, existed))
            continue
        log.warning("grab.unsafe_removed", infohash=infohash, release=release, reason=reason)
        try:
            await client.remove(infohash, delete_data=True)
        except Exception as exc:
            log.warning("grab.unsafe_remove_failed", infohash=infohash, detail=redact_exc(exc))
        dropped.append((infohash, release, reason))
    return safe, dropped


async def _grab_screened(
    client: DownloadClient, config: AppConfig, snap: _Snap, chosen: list[TorrentRelease]
) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str, str]]]:
    """Add magnets, screen them for malware metadata before they download, and record
    the verdict. Returns (kept, dropped). Refuses with :class:`_DiskRefusedError` before
    adding anything if the releases won't fit."""

    if config.torrent.min_free_gb and _download_dir(config, snap.kind):
        size = sum(rel.size_bytes or 0 for rel in chosen)
        refusal = _disk_refusal(config, snap.kind, await _in_flight_bytes(client), size)
        if refusal is not None:
            raise _DiskRefusedError(refusal)

    cid = snap.candidate_id
    safe, dropped = await _screen_grabs(client, await _grab(client, config, cid, snap.kind, chosen))
    stopped = set(_record_grabs(cid, safe, dropped))
    for infohash in stopped:
        await _drop_torrent(client, infohash)
    return [g for g in safe if g[0] not in stopped], dropped


def _set_tracking(
    s: Session, download_id: int, *, last_progress: float = 0.0, last_progress_at: datetime
) -> DownloadTracking:
    """Start (or restart) a download's clock. Everything else about its past goes."""

    track = s.get(DownloadTracking, download_id)
    if track is None:
        track = DownloadTracking(download_id=download_id, last_progress_at=last_progress_at)
        s.add(track)
    track.last_progress = last_progress
    track.last_progress_at = last_progress_at
    track.last_checked_at = None
    track.import_attempts = 0
    track.next_import_at = None
    return track


def _write_row(
    s: Session, candidate_id: int, infohash: str, release: str, state: str, error: str | None
) -> None:
    """Create or update the one row for (candidate, hash).

    A ban (``error``) or a stop (``cancelled``) lands on anything but imported history.
    A plain grab revives a finished row, since nothing else would ever look at it
    again (an imported one comes back when its file was lost; re-importing is safe),
    but never a ban.
    """

    existing = s.scalar(
        sa_select(Download).where(
            Download.candidate_id == candidate_id, Download.external_id == infohash
        )
    )
    if existing is None:
        s.add(
            Download(
                candidate_id=candidate_id,
                external_id=infohash,
                release=release,
                state=state,
                progress=0.0,
                error=error,
            )
        )
    elif error is not None or state == "cancelled":
        if existing.state != "imported":
            existing.state = state
            existing.error = error if error is not None else existing.error
    elif existing.state not in LIVE_DOWNLOAD_STATES and not (existing.error or "").startswith(
        MARKER_REMOVED
    ):
        existing.state = state
        existing.error = None
        existing.progress = 0.0
        existing.release = release
        existing.completed_at = None
        _set_tracking(s, existing.id, last_progress_at=utcnow())


def _record_added(candidate_id: int, infohash: str, release: str) -> bool:
    """Write the row for a magnet just added. False if the candidate was stopped in
    the meantime: the row is then ``cancelled``, and the torrent has to go."""

    def write() -> bool:
        with session_scope() as s:
            cand = s.get(Candidate, candidate_id)
            stopped = cand is None or cand.status in STOPPED_STATUSES
            _write_row(
                s, candidate_id, infohash, release, "cancelled" if stopped else "downloading", None
            )
            return not stopped

    try:
        return write()
    except IntegrityError:  # lost a race for the unique row; it exists now, so update it
        return write()


def _record_grabs(
    candidate_id: int,
    grabbed: list[tuple[str, str, bool]],
    dropped: list[tuple[str, str, str]] | None = None,
) -> list[str]:
    """Settle what a grab came to, idempotent on infohash. Safe grabs become active
    downloads and move the candidate to ``queued``; ``dropped`` malware grabs are
    recorded as failed ban rows (``removed: ...``) so the infohash is never
    re-selected. A candidate with no live download is left as it is, to retry the
    next-best release on the following cycle.

    Reads the candidate in this transaction, however long the grab took: one that has
    been rejected or cancelled meanwhile keeps its status, and its grabs are written
    ``cancelled``. Returns those hashes; the caller must remove the torrents."""

    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        stopped = cand is None or cand.status in STOPPED_STATUSES
        state = "cancelled" if stopped else "downloading"
        for infohash, release, _existed in grabbed:
            _write_row(s, candidate_id, infohash, release, state, None)
        for infohash, release, reason in dropped or []:
            _write_row(s, candidate_id, infohash, release, "failed", f"{MARKER_REMOVED} {reason}")
        s.flush()

        if stopped:
            return [infohash for infohash, _release, _existed in grabbed]
        live = s.scalar(
            sa_select(Download.id).where(
                Download.candidate_id == candidate_id,
                Download.state.in_(LIVE_DOWNLOAD_STATES),
            )
        )
        if cand is not None and live is not None:
            cand.status = CandidateStatus.queued
            if cand.decided_at is None:
                cand.decided_at = utcnow()
    return []


# When a season's episode count isn't known, probe episodes until this many
# consecutive numbers find no qualifying release; hard cap as a backstop.
_EPISODE_PROBE_MISSES = 2
_EPISODE_PROBE_CAP = 30

# TMDb series statuses that mean "no more episodes are coming" — a season short
# of its announced count is complete, not still topping up.
_ENDED_STATUSES = frozenset({"ended", "canceled", "cancelled"})


def _quarantined_hashes() -> frozenset[str]:
    """Infohashes previously removed as malware/junk — never grab these again.

    The quarantine keeps the failed ``Download`` row (error ``removed: …``) as
    the ban record; the same fake keeps resurfacing in searches under a
    clean-looking name.
    """

    with session_scope() as s:
        rows = s.scalars(
            # Any state: the screen marks failed, but a user ✖ Cancel of a malware
            # item leaves it cancelled — both carry the "removed:" ban marker.
            sa_select(Download.external_id).where(Download.error.like("removed:%"))
        ).all()
    return frozenset(h.lower() for h in rows if h)


def _dead_hashes() -> frozenset[str]:
    """Infohashes that went nowhere: still eligible, but last in line."""

    with session_scope() as s:
        rows = s.scalars(
            sa_select(Download.external_id).where(
                Download.state == "failed", Download.error.like(f"{MARKER_STALLED}%")
            )
        ).all()
    return frozenset(h.lower() for h in rows if h)


def _owned_episodes(title_id: int, season: int) -> set[int]:
    """Episode numbers already on the NAS for this season (multi-episode files
    expand to their range) — never re-download what the library has."""

    with session_scope() as s:
        rows = s.execute(
            sa_select(OwnedFile.episode, OwnedFile.episode_end).where(
                OwnedFile.title_id == title_id,
                OwnedFile.season == season,
                OwnedFile.episode.is_not(None),
            )
        ).all()
    episodes: set[int] = set()
    for episode, end in rows:
        episodes.update(range(episode, (end or episode) + 1))
    return episodes


def _grabbed_episodes(candidate_id: int, title_id: int, season: int) -> set[int] | None:
    """Episodes already covered by live downloads or owned files; ``None`` means
    a season pack is in flight.

    Failed/cancelled rows don't count — their episodes are up for re-grab — and nor
    do imported ones: what they delivered is in the owned episodes, and whatever
    they lacked is still up for grabs.
    """

    with session_scope() as s:
        rows = s.scalars(
            sa_select(Download.release).where(
                Download.candidate_id == candidate_id,
                Download.state.in_(LIVE_DOWNLOAD_STATES),
            )
        ).all()
    episodes = _owned_episodes(title_id, season)
    for name in rows:
        if not name:
            continue
        _seasons, eps = parse_season_episode(name)
        if eps:
            episodes.update(eps)
        elif is_season_pack(name):
            return None
        # Anything else (odd naming, "S02 Part 1") isn't a pack: treating it as one
        # would declare the season covered and stop top-ups. It just doesn't count.
    return episodes


def _season_target(candidate_id: int) -> int | None:
    """The season's announced episode count, snapshotted at discovery time."""

    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        if cand is None or not cand.features:
            return None
        target = cand.features.get("season_episodes")
        return int(target) if target else None


async def _aired_seasons(config: AppConfig, title_id: int) -> list[Any]:
    """Aired seasons (number >= 1, first episode already out) of a series, from
    TMDb details (cached). Empty when the title has no TMDb id / no data."""

    from ...metadata.tmdb import TMDbClient

    with session_scope() as s:
        title = s.get(Title, title_id)
        tmdb_id = title.tmdb_id if title is not None else None
    if tmdb_id is None or config.secrets.tmdb_api_key is None:
        return []
    async with httpx.AsyncClient(timeout=15.0) as http:
        tmdb = TMDbClient(
            config.secrets.tmdb_api_key.get_secret_value(),
            http,
            language=config.metadata.language,
            cache_days=config.metadata.cache_days,
        )
        details = await tmdb.details(tmdb_id, TitleKind.series)
    today = utcnow().date().isoformat()
    return sorted(
        (sn for sn in details.seasons if sn.number >= 1 and sn.air_date and sn.air_date <= today),
        key=lambda sn: sn.number,
    )


def _grabbable_seasons(title_id: int, aired: list[Any]) -> list[Any]:
    """Aired seasons to create candidates for: not on the NAS, and with no candidate
    already holding them, a rejection included."""

    from ...discovery.service import BLOCKING_STATUSES

    with session_scope() as s:
        taken = set(
            s.scalars(
                sa_select(Candidate.season).where(
                    Candidate.title_id == title_id,
                    Candidate.season.is_not(None),
                    Candidate.status.in_(BLOCKING_STATUSES),
                )
            ).all()
        )
    return [
        sn
        for sn in aired
        if sn.number not in taken and not _season_fully_owned(title_id, sn.number, sn.episode_count)
    ]


def _season_fully_owned(title_id: int, season: int, announced: int | None) -> bool:
    """True when the NAS already holds this whole season — so a whole-series grab
    shouldn't re-create a candidate for it. With no announced count, any owned
    episode is treated as complete (conservative: don't re-grab what you have)."""

    owned = _owned_episodes(title_id, season)
    if not owned:
        return False
    return len(owned) >= announced if announced else True


async def _expand_series(config: AppConfig, snap: _Snap) -> list[int]:
    """Turn a title-level series candidate into per-season candidates.

    The candidate itself becomes the first aired season; every other aired
    season gets an ``approved`` sibling (unless one already exists). Returns the
    candidate ids to grab, first one first. Empty when TMDb has no season data
    (caller falls back to the plain title grab).
    """

    seasons = _grabbable_seasons(snap.title_id, await _aired_seasons(config, snap.title_id))
    if not seasons:
        return []

    def season_feats(base: dict[str, Any] | None, sn: Any) -> dict[str, Any]:
        feats = dict(base or {})
        feats["season"] = sn.number
        feats.pop("season_episodes", None)  # the parent's count isn't this season's
        if sn.episode_count:
            feats["season_episodes"] = sn.episode_count
        return feats

    ids: list[int] = []
    with session_scope() as s:
        cand = s.get(Candidate, snap.candidate_id)
        title = s.get(Title, snap.title_id)
        if cand is None or title is None:
            return []
        first = seasons[0]
        cand.season = first.number
        cand.features = season_feats(cand.features, first)
        ids.append(cand.id)
        for sn in seasons[1:]:
            sibling = Candidate(
                title_id=cand.title_id,
                season=sn.number,
                source=cand.source,
                status=CandidateStatus.approved,
                reason=f"season S{sn.number:02d} of {snap.title} (series approved)",
                score=cand.score,
                features=season_feats(extract_features(title), sn),
                decided_at=utcnow(),
            )
            s.add(sibling)
            s.flush()
            ids.append(sibling.id)
    log.info("acquire.series_expanded", candidate=snap.candidate_id, seasons=len(ids))
    return ids


def _mark_imported(candidate_id: int) -> None:
    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        if cand is not None and cand.status not in STOPPED_STATUSES:
            cand.status = CandidateStatus.imported


async def _queue_series(config: AppConfig, snap: _Snap) -> QueueOutcome | None:
    """Grab a whole series: expand into seasons and queue each. ``None`` means
    no season data — use the plain title grab instead."""

    if config.features.dry_run:
        aired = await _aired_seasons(config, snap.title_id)
        if not aired:
            return None
        seasons = _grabbable_seasons(snap.title_id, aired)
        if not seasons:
            return QueueOutcome(
                snap.candidate_id,
                False,
                True,
                None,
                "all aired seasons already owned or in progress",
            )
        listed = ", ".join(f"S{sn.number:02d}" for sn in seasons)
        return QueueOutcome(
            snap.candidate_id, False, True, None, f"would grab {len(seasons)} seasons ({listed})"
        )

    ids = await _expand_series(config, snap)
    if not ids:
        # No unowned aired seasons to grab. If TMDb had seasons at all, report
        # that rather than falling through to a plain whole-title search (which
        # would re-grab a show you already fully own). Fully owned is done: staying
        # ``approved`` would only re-run this check on every acquire.
        aired = await _aired_seasons(config, snap.title_id)
        if aired:
            if all(_season_fully_owned(snap.title_id, n.number, n.episode_count) for n in aired):
                _mark_imported(snap.candidate_id)
            return QueueOutcome(
                snap.candidate_id,
                False,
                config.features.dry_run,
                None,
                "all aired seasons already owned or in progress",
            )
        return None
    parts: list[str] = []
    queued = False
    for cid in ids:
        outcome = await queue_candidate_torrent(config, cid)
        queued = queued or outcome.queued
        season = _load_snap(cid)
        label = f"S{season.season:02d}" if season and season.season is not None else str(cid)
        parts.append(f"{label}: {outcome.message}")
    return QueueOutcome(snap.candidate_id, queued, False, None, "; ".join(parts))


async def _queue_season(
    config: AppConfig,
    http: httpx.AsyncClient,
    sources: list[TorrentSource],
    snap: _Snap,
    allowed: list[str],
) -> QueueOutcome:
    """Grab a season-scoped candidate: the season pack if one exists, else the
    individual episodes that are out (an airing season has no pack yet)."""

    cid, n = snap.candidate_id, snap.season
    assert n is not None
    min_seeders = config.torrent.min_seeders
    banned = _quarantined_hashes()
    dead = _dead_hashes()
    have = _grabbed_episodes(cid, snap.title_id, n)
    if have is None:  # a pack download is live; nothing to add
        return QueueOutcome(
            cid, False, config.features.dry_run, None, "season pack already grabbed"
        )
    target = _season_target(cid)
    if target and len(_owned_episodes(snap.title_id, n)) >= target:
        # Everything is on the NAS already (a reopened season, say). Done, or this
        # would search for nothing on every acquire.
        if not config.features.dry_run:
            _mark_imported(cid)
        return QueueOutcome(cid, False, config.features.dry_run, None, "season already owned")

    if not have:  # nothing live yet: a full season pack beats episode-by-episode
        for query in (
            build_query(snap.title, snap.year, snap.kind, season=n),
            season_words_query(snap.title, n),
        ):
            releases = await _search_all(config, sources, query, snap.kind)
            chosen = select_release(
                releases,
                allowed_resolutions=allowed,
                min_seeders=min_seeders,
                season=n,
                banned=banned,
                dead=dead,
                require_hd=config.torrent.require_hd,
                expected_title=snap.title,
            )
            if chosen is not None:
                if config.features.dry_run:
                    would = (
                        f"would grab '{chosen.title}' from {chosen.source} "
                        f"({chosen.seeders} seeders)"
                    )
                    return QueueOutcome(cid, False, True, None, would)
                client = _download_client(config, http)
                grabbed, dropped = await _grab_screened(client, config, snap, [chosen])
                if not grabbed:
                    reason = dropped[0][2] if dropped else "no release grabbed"
                    return QueueOutcome(
                        cid, False, False, None, f"dropped pack: {reason}; will retry"
                    )
                return QueueOutcome(cid, True, False, None, f"grabbed season pack '{chosen.title}'")

    # No pack (season still airing, most likely): grab available episodes. A known
    # count says how far to look, gaps or not; otherwise stop at the first run of
    # misses past anything we know exists.
    last = target or _EPISODE_PROBE_CAP
    found: list[tuple[int, TorrentRelease]] = []
    misses = 0
    for e in range(1, last + 1):
        if e in have:
            continue
        query = build_query(snap.title, None, snap.kind, season=n, episode=e)
        releases = await _search_all(config, sources, query, snap.kind)
        chosen = select_release(
            releases,
            allowed_resolutions=allowed,
            min_seeders=min_seeders,
            season=n,
            episode=e,
            banned=banned,
            dead=dead,
            require_hd=config.torrent.require_hd,
            expected_title=snap.title,
        )
        if chosen is None:
            misses += 1
            known_max = max({*have, *(f for f, _ in found), 0})
            if target is None and misses >= _EPISODE_PROBE_MISSES and e > known_max:
                break
            continue
        misses = 0
        found.append((e, chosen))

    if not found:
        detail = "no season pack" + (f", {len(have)} episodes already grabbed" if have else "")
        return QueueOutcome(
            cid,
            False,
            config.features.dry_run,
            None,
            f"no suitable release for '{snap.title}' S{n:02d} ({detail})",
        )

    eps = ", ".join(f"E{e:02d}" for e, _ in found)
    if config.features.dry_run:
        return QueueOutcome(
            cid, False, True, None, f"would grab {len(found)} episode releases (S{n:02d} {eps})"
        )

    client = _download_client(config, http)
    grabbed, dropped = await _grab_screened(client, config, snap, [rel for _, rel in found])
    if not grabbed:
        reason = dropped[0][2] if dropped else "no release grabbed"
        return QueueOutcome(cid, False, False, None, f"dropped episodes: {reason}; will retry")
    n_ok = len(grabbed)
    drop_note = f"; dropped {len(dropped)} as malware" if dropped else ""
    suffix = "" if target and len(have) + n_ok >= target else "; will top up as more air"
    return QueueOutcome(
        cid,
        True,
        False,
        None,
        f"grabbed {n_ok} episode releases (S{n:02d} {eps}){drop_note}{suffix}",
    )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def sync_downloads_torrent(config: AppConfig) -> SyncStats:
    """Poll Transmission for in-flight torrents, import completed ones, advance state.

    On completion a movie is copied into the NAS library (see :mod:`.importer`);
    the candidate only reaches ``imported`` once that copy succeeds, so a failed
    import leaves the download in ``completed`` to be retried next sweep. Mirrors
    the arr sync otherwise: never resurrects a stopped candidate, and a torrent
    the client no longer knows, or that makes no progress for
    ``torrent.stale_after_hours``, flips to ``failed`` and leaves the client.
    """

    stale_after = timedelta(hours=config.torrent.stale_after_hours)
    watch_gap = max(_MIN_WATCH_GAP, timedelta(minutes=3 * config.schedule.sync_interval_minutes))
    _reap_orphaned_candidates(config)
    with session_scope() as s:
        # Include "completed": a torrent that finished but whose import failed sits
        # there awaiting a retry.
        rows: list[tuple[int, str]] = []
        meta: dict[int, tuple[int, TitleKind, str, int | None, int | None]] = {}
        for dl, title, season in s.execute(
            sa_select(Download, Title, Candidate.season)
            .join(Candidate, Candidate.id == Download.candidate_id)
            .join(Title, Title.id == Candidate.title_id)
            .where(Download.state.in_(LIVE_DOWNLOAD_STATES))
        ).all():
            if not dl.external_id:
                continue
            rows.append((dl.id, dl.external_id))
            meta[dl.id] = (title.id, title.kind, title.title, title.year, season)

    stats = SyncStats()
    # A large NAS copy that drops the SMB mount breaks every later import in the
    # run; don't cascade — once the mount is gone, defer the rest to the next
    # sweep. A per-file failure (bad name, stuck .part) must NOT block the others.
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

            # Safety scan before anything else (including import): a torrent whose
            # metadata reveals executables or no media at all is removed on sight.
            if st is not None and (unsafe := _unsafe_content(st.files)) is not None:
                await _quarantine(client, download_id, infohash, unsafe, stats)
                continue

            if st is not None and st.complete:
                if imports_blocked or not _import_due(download_id):
                    continue  # deferred, backing off, or parked for a person
                title_id, kind, title, year, season = meta[download_id]
                err = await _finish_completed(
                    config,
                    client,
                    stats,
                    download_id,
                    infohash,
                    st,
                    title_id,
                    kind,
                    title,
                    year,
                    season,
                )
                if err is not None and (mount_lost(config) or err.startswith("NAS unavailable")):
                    # The SMB link faulted (or dropped): stop importing this sweep
                    # so we don't thrash a faulting NAS file by file. Next sweep,
                    # ensure_mounted self-heals a dropped mount and resume kicks in.
                    imports_blocked = True
                    log.warning("sync.imports_deferred", reason=err)
                continue

            if _advance_download(download_id, st, stale_after, watch_gap, stats) and st is not None:
                await _drop_torrent(client, infohash)

        if config.torrent.delete_local_after_import:
            for download_id, infohash in _pending_removals():
                await _remove_local_copy(config, client, download_id, infohash)
    return stats


def _advance_download(
    download_id: int,
    st: TorrentStatus | None,
    stale_after: timedelta,
    watch_gap: timedelta,
    stats: SyncStats,
) -> bool:
    """Apply one poll of an unfinished torrent. True if the download is over, so
    the torrent should leave the client."""

    with session_scope() as s:
        dl = s.get(Download, download_id)
        if dl is None:
            return False
        cand = s.get(Candidate, dl.candidate_id)
        stale = _watch_progress(s, dl, st, watch_gap) > stale_after
        if cand is not None and cand.status in STOPPED_STATUSES:
            dl.state = "cancelled"
            return True
        if st is not None and st.error and st.error_is_local:
            dl.error = f"client error: {redact(st.error)}"
        elif stale:
            # Gone from the client, stopped, or going nowhere (dead magnet, no
            # seeders): Transmission reports such a torrent as active forever.
            where = "gone from the client" if st is None else "still in the client"
            dl.error = f"{MARKER_STALLED} no progress for {stale_after}, {where}"
        else:
            if st is not None and st.downloading:
                dl.state = "downloading"
                dl.progress = st.progress
                dl.save_path = st.save_path
                if cand is not None:
                    cand.status = CandidateStatus.downloading
                stats.downloading += 1
            return False
        dl.state = "failed"
        # One dead episode must not fail a candidate whose siblings are still
        # transferring.
        if cand is not None:
            _settle_after_loss(s, cand, dl.id)
        stats.failed += 1
        return True


def _watch_progress(
    s: Session, dl: Download, st: TorrentStatus | None, watch_gap: timedelta
) -> timedelta:
    """Update a download's no-progress clock and return how long it has been going.

    Only time we watched counts: not time waiting in the client's own queue, and not
    a gap between sweeps long enough that the host was probably asleep, or a torrent
    would fail the moment the Mac wakes up. A row from before this clock existed
    has no history to go on: with progress on record it starts now, without it
    (a dead magnet) it has been waiting since it was grabbed.
    """

    now = utcnow()
    track = s.get(DownloadTracking, dl.id)
    if track is None:
        start = now if dl.progress else _aware(dl.created_at)
        track = _set_tracking(s, dl.id, last_progress=dl.progress or 0.0, last_progress_at=start)
        s.flush()
    moved_at = _aware(track.last_progress_at)
    if track.last_checked_at is not None:
        gap = now - _aware(track.last_checked_at)
        if gap > watch_gap:
            moved_at += gap
    # A hand-paused torrent is a decision, not a stall: never fail or delete it.
    if st is not None and (st.queued or st.paused):
        moved_at = now
    if st is not None and st.progress > track.last_progress:
        track.last_progress = st.progress
        moved_at = now
    track.last_progress_at = moved_at
    track.last_checked_at = now
    return now - moved_at


def _settle_after_loss(s: Session, cand: Candidate, download_id: int) -> None:
    """A candidate has lost a download. Once none are live: a movie is ``failed``, for
    a person to retry; a season still has weeks of top-ups ahead of it, so it goes
    back to ``approved`` with a growing backoff, and ``failed`` only when that runs
    out. Otherwise a followed show would just stop, with nothing to say why.
    """

    if cand.status in STOPPED_STATUSES or _live_sibling(s, cand.id, download_id) is not None:
        return
    if cand.season is None:
        cand.status = CandidateStatus.failed
        return
    target = (cand.features or {}).get("season_episodes")
    covered, pack = _season_coverage(s, cand, None)
    if pack or (target and len(covered) >= int(target)):
        cand.status = CandidateStatus.imported  # what was lost isn't needed any more
        return
    retry = s.get(GrabRetry, cand.id)
    attempts = retry.attempts + 1 if retry is not None else 1
    if attempts > _GRAB_RETRY_MAX:
        cand.status = CandidateStatus.failed
        return
    wait = min(_GRAB_RETRY_BACKOFF * 2 ** (attempts - 1), _GRAB_RETRY_BACKOFF_MAX)
    if retry is None:
        retry = GrabRetry(candidate_id=cand.id, next_attempt_at=utcnow() + wait)
        s.add(retry)
    retry.attempts = attempts
    retry.next_attempt_at = utcnow() + wait
    cand.status = CandidateStatus.approved


def _reap_orphaned_candidates(config: AppConfig) -> None:
    """Settle candidates marked in flight with no live download behind them, which
    nothing else would ever look at again. A grab holds the acquire lock while its
    claim has no rows yet, so that's left alone."""

    if is_job_running(config, "acquire"):
        return
    live = sa_select(Download.candidate_id).where(Download.state.in_(LIVE_DOWNLOAD_STATES))
    with session_scope() as s:
        orphans = s.scalars(
            sa_select(Candidate).where(
                Candidate.status.in_((CandidateStatus.queued, CandidateStatus.downloading)),
                Candidate.id.not_in(live),
            )
        ).all()
        for cand in orphans:
            log.warning("sync.orphaned_candidate", candidate=cand.id, status=cand.status.value)
            delivered = s.scalar(
                sa_select(Download.id).where(
                    Download.candidate_id == cand.id, Download.state == "imported"
                )
            )
            if delivered is None:
                _settle_after_loss(s, cand, 0)
            else:
                cand.status = _status_after_finished(s, cand, 0, None)


# File extensions that mean "this torrent is malware bait, not media". A movie
# release has no business containing any of these.
_EXECUTABLE_EXTS = frozenset(
    {".exe", ".scr", ".bat", ".cmd", ".com", ".msi", ".pif", ".vbs", ".jar", ".apk", ".dmg", ".lnk"}
)


def _unsafe_content(files: list[str] | None) -> str | None:
    """Why this torrent's file list must not be downloaded, or ``None`` if fine.

    ``None``/empty input means magnet metadata hasn't resolved yet — not a
    verdict. Filenames are stripped: fakes pad spaces before the extension
    ("Movie 1080p .exe") to hide it in UIs.
    """

    from ...scanner.parse import is_media_file

    if not files:
        return None
    # A real release contains the actual video. If it does, keep it even when a
    # small installer/junk .exe rides along (common in scene/repacks) — nuking a
    # completed multi-GB download over a 0-byte stub is worse than the risk.
    # The dangerous fakes are the ones with NO media at all: a lone .exe wearing
    # a movie name, or a rar-only bundle.
    if any(is_media_file(name.strip()) for name in files):
        return None
    for name in files:
        clean = name.strip()
        ext = os.path.splitext(clean)[1].strip().lower()
        if ext in _EXECUTABLE_EXTS:
            exe = os.path.basename(clean)
            return f"executable {exe!r} and no media (likely malware)"
    return "contains no media file"


async def _quarantine(
    client: DownloadClient, download_id: int, infohash: str, reason: str, stats: SyncStats
) -> None:
    """Remove a malicious/junk torrent (with its data) and fail its download."""

    log.warning("sync.unsafe_torrent", download=download_id, infohash=infohash, reason=reason)
    try:
        await client.remove(infohash, delete_data=True)
    except Exception as exc:  # keep the DB verdict even if the client call fails
        log.warning("sync.unsafe_remove_failed", infohash=infohash, detail=redact_exc(exc))
    with session_scope() as s:
        dl = s.get(Download, download_id)
        if dl is None:
            return
        dl.state = "failed"
        dl.error = f"{MARKER_REMOVED} torrent {reason}"
        cand = s.get(Candidate, dl.candidate_id)
        if cand is not None:
            _settle_after_loss(s, cand, dl.id)
    stats.failed += 1
    stats.errors.append(f"{infohash}: {reason}")


def _live_sibling(s: Session, candidate_id: int, download_id: int) -> int | None:
    """Id of another still-in-flight download for this candidate, if any."""

    return s.scalar(
        sa_select(Download.id).where(
            Download.candidate_id == candidate_id,
            Download.id != download_id,
            Download.state.in_(LIVE_DOWNLOAD_STATES),
        )
    )


def _coverage_from_releases(s: Session, candidate_id: int) -> tuple[set[int], bool]:
    """(episodes, has a pack) going by release names, for when nothing is copied to
    the library so there are no imported episodes to count."""

    covered: set[int] = set()
    pack = False
    for name in s.scalars(
        sa_select(Download.release).where(
            Download.candidate_id == candidate_id, Download.state == "imported"
        )
    ).all():
        _seasons, eps = parse_season_episode(name or "")
        if eps:
            covered.update(eps)
        elif is_season_pack(name or ""):
            pack = True
    return covered, pack


def _season_coverage(
    s: Session, cand: Candidate, imported: list[EpisodeImport] | None
) -> tuple[set[int], bool]:
    """(episodes in the library, a pack was delivered). ``imported`` is what this
    download just placed; None when nothing is copied (import switched off), and
    release names are then all there is to go on."""

    assert cand.season is not None
    pack = False
    if imported is None:
        covered, pack = _coverage_from_releases(s, cand.id)
    else:
        covered = {
            number
            for ep in imported
            if ep.season == cand.season and ep.episode is not None
            for number in range(ep.episode, (ep.episode_end or ep.episode) + 1)
        }
    return covered | _owned_episodes(cand.title_id, cand.season), pack


def _lost_episode(s: Session, cand: Candidate, covered: set[int]) -> bool:
    """Did a failed download leave an episode that nothing else delivered?"""

    for name in s.scalars(
        sa_select(Download.release).where(
            Download.candidate_id == cand.id, Download.state == "failed"
        )
    ).all():
        if set(parse_season_episode(name or "")[1]) - covered:
            return True
    return False


def _status_after_finished(
    s: Session, cand: Candidate, download_id: int, imported: list[EpisodeImport] | None
) -> CandidateStatus:
    """Candidate status once one of its downloads has imported.

    Siblings still transferring keep it ``downloading``. A season candidate with
    episodes still missing (vs. the season's announced count) returns to
    ``approved`` so the next acquire run tops it up — an airing season arrives
    week by week. One left with a hole where a download failed ends ``failed``, so
    Retry shows, rather than ``imported`` with an episode quietly missing. Otherwise:
    ``imported``.

    Coverage is what actually reached the library: the owned episodes plus this
    download's ``imported`` ones. A release called "Season 3 COMPLETE" proves
    nothing. ``imported`` is None when library import is switched off.
    """

    if _live_sibling(s, cand.id, download_id) is not None:
        return CandidateStatus.downloading
    if cand.season is None:
        return CandidateStatus.imported
    covered, pack = _season_coverage(s, cand, imported)
    if pack:
        return CandidateStatus.imported
    target = (cand.features or {}).get("season_episodes")
    if target and len(covered) < int(target):
        # Keep topping up only while the show is still airing. If it has
        # ended, the announced count can exceed what actually exists (TMDb
        # overcount / shortened season) — completing avoids a candidate that
        # re-searches a phantom tail every acquire cycle forever.
        title = s.get(Title, cand.title_id)
        ended = bool(
            title and title.series_status and title.series_status.lower() in _ENDED_STATUSES
        )
        if not ended:
            return CandidateStatus.approved
    if _lost_episode(s, cand, covered):
        return CandidateStatus.failed
    return CandidateStatus.imported


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


def _import_due(download_id: int) -> bool:
    """False while an import is backing off or parked for a person."""

    with session_scope() as s:
        dl = s.get(Download, download_id)
        if dl is None or (dl.error or "").startswith(MARKER_NEEDS_ATTENTION):
            return False
        track = s.get(DownloadTracking, download_id)
        return (
            track is None
            or track.next_import_at is None
            or _aware(track.next_import_at) <= utcnow()
        )


def _claim_import(download_id: int) -> bool:
    """Atomically move a live download to ``importing``, from which restart and cancel
    keep off. False if it stopped being live since the sweep listed it, or its
    candidate was stopped."""

    running = sa_select(Candidate.id).where(Candidate.status.not_in(STOPPED_STATUSES))
    with session_scope() as s:
        result = s.execute(
            sa_update(Download)
            .where(
                Download.id == download_id,
                Download.state.in_(LIVE_DOWNLOAD_STATES),
                Download.candidate_id.in_(running),
            )
            .values(state="importing", progress=0.0)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]


def _note_import_failure(download_id: int, error: str, *, park: bool, counted: bool) -> None:
    """Record a failed import: ``completed`` with the error, retried with a growing
    gap. ``park`` gives up now; ``counted`` is False for a fault that isn't this
    item's. The attempt cap parks it too."""

    with session_scope() as s:
        dl = s.get(Download, download_id)
        if dl is None:
            return
        dl.state = "completed"  # torrent done; import pending retry
        dl.progress = 1.0
        dl.error = error
        if counted:
            track = s.get(DownloadTracking, download_id)
            if track is None:
                track = _set_tracking(s, download_id, last_progress=1.0, last_progress_at=utcnow())
            track.import_attempts += 1
            attempts = track.import_attempts
            wait = min(_IMPORT_BACKOFF * 2 ** (attempts - 1), _IMPORT_BACKOFF_MAX)
            track.next_import_at = utcnow() + wait
            park = park or attempts >= _IMPORT_MAX_ATTEMPTS
        if park:
            dl.error = f"{MARKER_NEEDS_ATTENTION} {error}"


async def _run_copy[T](cancel: threading.Event, fn: Callable[[], T]) -> T:
    """Run a library copy in a worker thread, and don't come back before it has
    stopped. Cancelling this task can't stop a thread, and returning early would let
    the caller release the sync lock with the copy still writing, so a cancel asks
    the copy to stop, then waits for it. That wait has no limit by design: a copy
    stuck in a dead NAS write can't be interrupted, and the lock staying held is the
    truth of the matter until the process goes."""

    started, finished = threading.Event(), threading.Event()

    def run() -> T:
        started.set()
        try:
            return fn()
        finally:
            finished.set()

    job = asyncio.ensure_future(asyncio.to_thread(run))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        cancel.set()
        log.warning("import.stopping")
        while not finished.is_set() and not (job.done() and not started.is_set()):
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(_THREAD_POLL_S)
        job.add_done_callback(lambda t: t.cancelled() or t.exception())
        raise


def _pending_removals() -> list[tuple[int, str]]:
    """Imported downloads whose torrent removal never finished: (download id, hash)."""

    with session_scope() as s:
        rows = s.execute(
            sa_select(Download.id, Download.external_id).where(
                Download.state == "imported", Download.error == _REMOVAL_PENDING
            )
        ).all()
    return [(download_id, infohash) for download_id, infohash in rows if infohash]


async def _remove_local_copy(
    config: AppConfig, client: DownloadClient, download_id: int, infohash: str
) -> None:
    """Remove an imported torrent and its local data, keeping the pending marker on
    failure so a later sweep retries. Safe to repeat: the client ignores a hash it
    no longer knows. Held back while the library mount is gone."""

    if mount_lost(config):
        log.warning("import.cleanup_deferred", download=download_id)
        return
    try:
        await client.remove(infohash, delete_data=True)
    except Exception as exc:
        log.warning("import.cleanup_failed", download=download_id, detail=redact_exc(exc))
        return
    _set_download(download_id, error=None)


def _hold_message(held: list[tuple[str, int]]) -> str:
    names = ", ".join(os.path.basename(path) for path, _size in held[:3])
    more = f" and {len(held) - 3} more" if len(held) > 3 else ""
    return (
        f"held: {len(held)} video(s) were not imported ({names}{more}), so the local copy "
        "is kept. Rename an episode to SxxEyy, or remove what you don't want from the "
        "download folder, and the next sweep finishes this."
    )


def _catalog_import(
    config: AppConfig,
    download_id: int,
    title_id: int,
    release: str,
    dest: str | None,
    episodes: list[EpisodeImport],
    kind: TitleKind,
) -> list[int]:
    """Register imported files as owned; returns their ids. Best-effort: a failure
    here never un-imports the media."""

    try:
        if kind is TitleKind.movie:
            return [register_owned_movie(config, title_id, dest, release)] if dest else []
        return [register_owned_episode(config, title_id, ep) for ep in episodes]
    except Exception as exc:
        log.warning("import.catalog_failed", download=download_id, detail=redact_exc(exc))
        return []


def _upgrade_plan(
    download_id: int, load_plan: Callable[[int], UpgradePlan | None]
) -> UpgradePlan | None:
    with session_scope() as s:
        dl = s.get(Download, download_id)
        candidate_id = dl.candidate_id if dl is not None else None
    return load_plan(candidate_id) if candidate_id is not None else None


def _abandon_stopped(download_id: int) -> None:
    """The candidate behind a finished torrent was stopped: don't import it."""

    with session_scope() as s:
        dl = s.get(Download, download_id)
        if dl is not None and dl.state in LIVE_DOWNLOAD_STATES:
            dl.state = "cancelled"


async def _copy_to_library(
    config: AppConfig,
    download_id: int,
    infohash: str,
    content: str,
    kind: TitleKind,
    title: str,
    year: int | None,
    season: int | None,
    replaces: ReplacementGrant | None = None,
) -> tuple[str | None, list[EpisodeImport], list[tuple[str, int]]]:
    """Copy a finished torrent into the library in a worker thread, so the big NAS
    copy neither blocks the event loop nor hides its progress: it updates
    Download.progress, which the Activity view polls live. Returns (dest, episodes,
    videos held back). Advertises the copy for as long as it runs.

    ``replaces`` is an approved upgrade's old file. It goes to the trash here, after
    the import proved the new file at its final path and only if nothing was held
    back: a held video may be the second half of the movie."""

    from .importer import (
        build_library_target,
        find_unimported_videos,
        import_completed_episodes,
        import_completed_movie,
        retire_replaced_file,
    )

    target = build_library_target(config)
    cancel = threading.Event()
    on_progress = _import_progress_cb(download_id)

    def copy() -> tuple[str | None, list[EpisodeImport], list[tuple[str, int]]]:
        if kind is TitleKind.movie:
            dest = import_completed_movie(
                config,
                target,
                content_path=content,
                title=title,
                year=year,
                on_progress=on_progress,
                infohash=infohash,
                cancel=cancel,
                replaces=replaces,
            )
            held = find_unimported_videos(content)
            if replaces is not None and not held:
                retire_replaced_file(
                    config,
                    replaces,
                    dest,
                    reason=f"upgrade: replaced by {os.path.basename(dest)}",
                    cancel=cancel,
                )
            return dest, [], held
        episodes = import_completed_episodes(
            config,
            target,
            content_path=content,
            series_title=title,
            season=season,
            on_progress=on_progress,
            infohash=infohash,
            cancel=cancel,
        )
        folder = os.path.dirname(episodes[-1].dest) if episodes else None
        return folder, episodes, find_unimported_videos(content, [ep.source for ep in episodes])

    with import_active(config):
        return await _run_copy(cancel, copy)


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
    season: int | None,
) -> str | None:
    """Import a finished torrent into the library and mark it imported.

    Movies are copied into the NAS movie layout; series torrents (an episode or
    a whole season pack) place each media file into ``TV Shows/<Series>/Season
    NN/``. A failed import records the error and leaves state ``completed``, to
    retry with growing gaps and then park as ``needs_attention:`` (see
    :func:`_note_import_failure`) — the candidate is not advanced until the files are
    in place. Returns the error string on a failed import (else ``None``).

    A torrent with a real video that wasn't imported is held the same way: what
    did import is cataloged, but the download stays ``completed`` with a ``held:``
    error and its local data is left alone.

    ``imported`` commits before the torrent is removed, so a crash in between
    can't look like a torrent that vanished mid-import and trigger a re-grab; the
    next sweep finishes the removal.

    A download whose candidate was stopped since the sweep listed it isn't imported:
    it's cancelled and its torrent removed.

    An upgrade runs through here too. Its old file leaves as the last step of the
    copy, so a failure anywhere leaves the original in the library and this retries
    like any failed import: the verified new file passes as already in place, and
    the local data stays until ``imported``.
    """

    from ...upgrades import finish_upgrade, grant_for, load_plan

    if not _claim_import(download_id):
        _abandon_stopped(download_id)
        await _drop_torrent(client, infohash)
        return None

    dest: str | None = None
    episodes: list[EpisodeImport] = []
    error: str | None = None
    held: list[tuple[str, int]] = []
    park = False
    counted = False

    plan: UpgradePlan | None = None
    try:
        plan = _upgrade_plan(download_id, load_plan)
    except InvalidTransitionError as exc:
        error, park = f"import failed: {redact_exc(exc)}", True  # no plan, no safe way in

    content = st.content_path()
    if error is not None:
        pass
    elif not config.torrent.import_to_library:
        if plan is not None:
            error, park = "import failed: an upgrade needs torrent.import_to_library", True
        log.info("import.skipped_disabled", download=download_id, title=title)
    elif content is None:
        error = "client reported no content path"
    else:
        try:
            replaces = grant_for(config, plan) if plan is not None else None
            dest, episodes, held = await _copy_to_library(
                config, download_id, infohash, content, kind, title, year, season, replaces
            )
        except asyncio.CancelledError:
            # Stopped by the deadline or a shutdown, thread included. Nothing's wrong
            # with the item and the next sweep resumes it.
            _set_download(download_id, state="completed", progress=1.0)
            raise
        except NasUnavailableError as exc:
            error = f"NAS unavailable: {redact_exc(exc)}"
            log.warning("import.nas_unavailable", download=download_id, title=title, detail=error)
        except LibraryConflictError as exc:
            error = f"import failed: {redact_exc(exc)}"
            park = True  # retrying can't change this; a person has to pick a file
            log.warning("import.conflict", download=download_id, title=title, detail=error)
        except Exception as exc:
            error = f"import failed: {redact_exc(exc)}"
            counted = not mount_lost(config)
            log.warning("import.failed", download=download_id, title=title, detail=error)

    if error is not None:
        _note_import_failure(download_id, error, park=park, counted=counted)
        _set_download(download_id, save_path=st.save_path)
        stats.errors.append(f"{infohash}: {error}")
        return error

    # Catalog the file(s) so they're "owned" before completion is worked out below,
    # instead of waiting for a NAS rescan.
    owned_ids = _catalog_import(
        config, download_id, title_id, st.name or title, dest, episodes, kind
    )
    if plan is not None and not held:
        try:
            finish_upgrade(plan, title_id)
        except Exception as exc:  # the next scan prunes the old row anyway
            log.warning("import.catalog_failed", download=download_id, detail=redact_exc(exc))

    if held:
        message = _hold_message(held)
        log.warning("import.held", download=download_id, title=title, videos=len(held))
        _set_download(
            download_id, state="completed", progress=1.0, save_path=st.save_path, error=message
        )
        stats.errors.append(f"{infohash}: {message}")
        return message

    remove_local = dest is not None and config.torrent.delete_local_after_import
    with session_scope() as s:
        dl = s.get(Download, download_id)
        if dl is not None:
            dl.state = "imported"
            dl.progress = 1.0
            dl.save_path = dest or st.save_path
            dl.error = _REMOVAL_PENDING if remove_local else None
            dl.completed_at = utcnow()
            cand = s.get(Candidate, dl.candidate_id)
            if cand is not None and cand.status not in STOPPED_STATUSES:
                cand.status = _status_after_finished(
                    s, cand, dl.id, episodes if dest is not None else None
                )
                s.execute(sa_delete(GrabRetry).where(GrabRetry.candidate_id == cand.id))
    stats.completed += 1

    # F7 (best-effort): ask Kodi for a debounced library scan. Never fails an import.
    try:
        from ...kodi import note_import

        await note_import(config)
    except Exception as exc:
        log.info("import.kodi_note_failed", download=download_id, detail=redact_exc(exc))

    if remove_local:
        await _remove_local_copy(config, client, download_id, infohash)

    # Complete the pipeline: fetch subtitles now instead of waiting for a full sweep.
    # Best-effort — a failure here never un-imports the media.
    try:
        if config.subtitles.backend == "native":
            from ...subtitles.native.service import fetch_for_owned_file

            for owned_id in owned_ids:
                got = await fetch_for_owned_file(config, owned_id)
                log.info(
                    "import.subtitles",
                    download=download_id,
                    title=title,
                    owned_file=owned_id,
                    fetched=len(got),
                )
    except Exception as exc:
        log.warning("import.catalog_failed", download=download_id, detail=redact_exc(exc))
    return None
