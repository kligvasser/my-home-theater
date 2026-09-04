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
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .importer import EpisodeImport

import httpx
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

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
from .importer import NasUnavailableError, mount_lost
from .select import build_query, parse_season_episode, select_release
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
    are no-ops; rejected is an error. A season candidate that's ``approved``
    with downloads already recorded is an episode top-up, not a duplicate."""

    snap = _load_snap(candidate_id)
    if snap is None:
        raise ValueError(f"candidate {candidate_id} not found")

    top_up = snap.season is not None and snap.status is CandidateStatus.approved
    if snap.status in (
        CandidateStatus.queued,
        CandidateStatus.downloading,
        CandidateStatus.imported,
    ) or (snap.has_download and snap.status is not CandidateStatus.failed and not top_up):
        return QueueOutcome(
            candidate_id, False, config.features.dry_run, None, f"already {snap.status.value}"
        )
    if snap.status is CandidateStatus.rejected:
        raise InvalidTransitionError(
            f"candidate {candidate_id} was rejected; approve it again before queueing"
        )

    if snap.kind is TitleKind.series and snap.season is None:
        # Approving a series means the whole series: one season candidate each.
        outcome = await _queue_series(config, snap)
        if outcome is not None:
            return outcome

    async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
        sources = _build_sources(config, http)
        if not sources:
            raise NotConfiguredError(
                "No torrent sources enabled; set torrent.enabled_sources in config.yaml."
            )
        allowed = config.torrent.resolutions or config.thresholds.allowed_resolutions

        if snap.kind is TitleKind.series and snap.season is not None:
            return await _queue_season(config, http, sources, snap, allowed)

        query = build_query(snap.title, snap.year, snap.kind)
        releases = await _search_all(sources, query, snap.kind)
        chosen = select_release(
            releases,
            allowed_resolutions=allowed,
            min_seeders=config.torrent.min_seeders,
            banned=_quarantined_hashes(),
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

        client = _download_client(config, http)
        grabbed = await _grab(client, config, snap.kind, [chosen])

    _record_grabs(candidate_id, grabbed)
    already = all(existed for _, _, existed in grabbed)
    message = "grabbed (already in client)" if already else "grabbed"
    return QueueOutcome(candidate_id, True, False, None, message)


async def _grab(
    client: DownloadClient, config: AppConfig, kind: TitleKind, chosen: list[TorrentRelease]
) -> list[tuple[str, str, bool]]:
    """Add each release's magnet to the client → (infohash, release, existed)."""

    out: list[tuple[str, str, bool]] = []
    for rel in chosen:
        magnet = rel.magnet_uri()
        assert magnet is not None  # select_release only returns releases with a magnet
        added = await client.add_magnet(magnet, download_dir=_download_dir(config, kind))
        out.append((added.infohash, rel.title, added.already_existed))
    return out


def _record_grabs(candidate_id: int, grabbed: list[tuple[str, str, bool]]) -> None:
    """Record Download rows (idempotent on infohash) and mark the candidate queued."""

    with session_scope() as s:
        for infohash, release, _existed in grabbed:
            existing = s.scalar(
                sa_select(Download).where(
                    Download.candidate_id == candidate_id,
                    Download.external_id == infohash,
                )
            )
            if existing is None:
                s.add(
                    Download(
                        candidate_id=candidate_id,
                        external_id=infohash,
                        release=release,
                        state="downloading",
                        progress=0.0,
                    )
                )
        cand = s.get(Candidate, candidate_id)
        if cand is not None:
            cand.status = CandidateStatus.queued
            if cand.decided_at is None:
                cand.decided_at = utcnow()


# When a season's episode count isn't known, probe episodes until this many
# consecutive numbers find no qualifying release; hard cap as a backstop.
_EPISODE_PROBE_MISSES = 2
_EPISODE_PROBE_CAP = 30


def _quarantined_hashes() -> frozenset[str]:
    """Infohashes previously removed as malware/junk — never grab these again.

    The quarantine keeps the failed ``Download`` row (error ``removed: …``) as
    the ban record; the same fake keeps resurfacing in searches under a
    clean-looking name.
    """

    with session_scope() as s:
        rows = s.scalars(
            sa_select(Download.external_id).where(
                Download.state == "failed", Download.error.like("removed:%")
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

    Failed/cancelled rows don't count — their episodes are up for re-grab.
    """

    with session_scope() as s:
        releases = s.scalars(
            sa_select(Download.release).where(
                Download.candidate_id == candidate_id,
                Download.state.in_(("queued", "downloading", "importing", "completed", "imported")),
            )
        ).all()
    episodes = _owned_episodes(title_id, season)
    for name in releases:
        if not name:
            continue
        _seasons, eps = parse_season_episode(name)
        if not eps:  # no episode number on a live download: it's the season pack
            return None
        episodes.update(eps)
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


async def _expand_series(config: AppConfig, snap: _Snap) -> list[int]:
    """Turn a title-level series candidate into per-season candidates.

    The candidate itself becomes the first aired season; every other aired
    season gets an ``approved`` sibling (unless one already exists). Returns the
    candidate ids to grab, first one first. Empty when TMDb has no season data
    (caller falls back to the plain title grab).
    """

    from ...discovery.service import BLOCKING_STATUSES

    seasons = await _aired_seasons(config, snap.title_id)
    if not seasons:
        return []

    def season_feats(base: dict[str, Any] | None, sn: Any) -> dict[str, Any]:
        feats = dict(base or {})
        feats["season"] = sn.number
        if sn.episode_count:
            feats["season_episodes"] = sn.episode_count
        return feats

    ids: list[int] = []
    with session_scope() as s:
        cand = s.get(Candidate, snap.candidate_id)
        if cand is None:
            return []
        first = seasons[0]
        cand.season = first.number
        cand.features = season_feats(cand.features, first)
        ids.append(cand.id)
        for sn in seasons[1:]:
            exists = s.scalar(
                sa_select(Candidate.id).where(
                    Candidate.title_id == cand.title_id,
                    Candidate.season == sn.number,
                    Candidate.status.in_(BLOCKING_STATUSES),
                )
            )
            if exists is not None:
                continue
            sibling = Candidate(
                title_id=cand.title_id,
                season=sn.number,
                source=cand.source,
                status=CandidateStatus.approved,
                reason=f"season S{sn.number:02d} of {snap.title} (series approved)",
                score=cand.score,
                features=season_feats(cand.features, sn),
                decided_at=utcnow(),
            )
            s.add(sibling)
            s.flush()
            ids.append(sibling.id)
    log.info("acquire.series_expanded", candidate=snap.candidate_id, seasons=len(ids))
    return ids


async def _queue_series(config: AppConfig, snap: _Snap) -> QueueOutcome | None:
    """Grab a whole series: expand into seasons and queue each. ``None`` means
    no season data — use the plain title grab instead."""

    if config.features.dry_run:
        seasons = await _aired_seasons(config, snap.title_id)
        if not seasons:
            return None
        listed = ", ".join(f"S{sn.number:02d}" for sn in seasons)
        return QueueOutcome(
            snap.candidate_id, False, True, None, f"would grab {len(seasons)} seasons ({listed})"
        )

    ids = await _expand_series(config, snap)
    if not ids:
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
    have = _grabbed_episodes(cid, snap.title_id, n)
    if have is None:  # a pack download is live; nothing to add
        return QueueOutcome(
            cid, False, config.features.dry_run, None, "season pack already grabbed"
        )

    if not have:  # nothing live yet: a full season pack beats episode-by-episode
        for query in (
            build_query(snap.title, snap.year, snap.kind, season=n),
            f"{snap.title} Season {n}",
        ):
            releases = await _search_all(sources, query, snap.kind)
            chosen = select_release(
                releases,
                allowed_resolutions=allowed,
                min_seeders=min_seeders,
                season=n,
                banned=banned,
            )
            if chosen is not None:
                if config.features.dry_run:
                    would = (
                        f"would grab '{chosen.title}' from {chosen.source} "
                        f"({chosen.seeders} seeders)"
                    )
                    return QueueOutcome(cid, False, True, None, would)
                client = _download_client(config, http)
                grabbed = await _grab(client, config, snap.kind, [chosen])
                _record_grabs(cid, grabbed)
                return QueueOutcome(cid, True, False, None, f"grabbed season pack '{chosen.title}'")

    # No pack (season still airing, most likely): grab available episodes.
    target = _season_target(cid)
    last = target or _EPISODE_PROBE_CAP
    found: list[tuple[int, TorrentRelease]] = []
    misses = 0
    for e in range(1, last + 1):
        if e in have:
            continue
        releases = await _search_all(sources, f"{snap.title} S{n:02d}E{e:02d}", snap.kind)
        chosen = select_release(
            releases,
            allowed_resolutions=allowed,
            min_seeders=min_seeders,
            season=n,
            episode=e,
            banned=banned,
        )
        if chosen is None:
            misses += 1
            # Known target: later episodes may exist even after a gap. Unknown:
            # consecutive misses mean we've walked past the season's end.
            if target is None and misses >= _EPISODE_PROBE_MISSES:
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
    grabbed = await _grab(client, config, snap.kind, [rel for _, rel in found])
    _record_grabs(cid, grabbed)
    suffix = "" if target and len(have) + len(found) >= target else "; will top up as more air"
    return QueueOutcome(
        cid, True, False, None, f"grabbed {len(found)} episode releases (S{n:02d} {eps}){suffix}"
    )


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
        meta: dict[int, tuple[int, TitleKind, str, int | None, int | None]] = {}
        for dl, title, season in s.execute(
            sa_select(Download, Title, Candidate.season)
            .join(Candidate, Candidate.id == Download.candidate_id)
            .join(Title, Title.id == Candidate.title_id)
            .where(Download.state.in_(("queued", "downloading", "importing", "completed")))
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
                if imports_blocked:
                    continue  # deferred to the next sweep after an earlier failure
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
                    # One dead episode must not fail a candidate whose siblings are
                    # still transferring; with none left, failed makes it re-queueable
                    # (the next grab skips episodes that already imported).
                    if cand is not None and _live_sibling(s, cand.id, dl.id) is None:
                        cand.status = CandidateStatus.failed
                    stats.failed += 1
    return stats


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
    for name in files:
        clean = name.strip()
        ext = os.path.splitext(clean)[1].strip().lower()
        if ext in _EXECUTABLE_EXTS:
            return f"contains an executable ({os.path.basename(clean)!r}) — likely malware"
    if not any(is_media_file(name.strip()) for name in files):
        return "contains no media file"
    return None


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
        dl.error = f"removed: torrent {reason}"
        cand = s.get(Candidate, dl.candidate_id)
        if (
            cand is not None
            and cand.status is not CandidateStatus.rejected
            and _live_sibling(s, cand.id, dl.id) is None
        ):
            cand.status = CandidateStatus.failed
    stats.failed += 1
    stats.errors.append(f"{infohash}: {reason}")


def _live_sibling(s: Session, candidate_id: int, download_id: int) -> int | None:
    """Id of another still-in-flight download for this candidate, if any."""

    return s.scalar(
        sa_select(Download.id).where(
            Download.candidate_id == candidate_id,
            Download.id != download_id,
            Download.state.in_(("queued", "downloading", "importing", "completed")),
        )
    )


def _status_after_finished(s: Session, cand: Candidate, download_id: int) -> CandidateStatus:
    """Candidate status once one of its downloads has imported.

    Siblings still transferring keep it ``downloading``. A season candidate with
    episodes still missing (vs. the season's announced count) returns to
    ``approved`` so the next acquire run tops it up — an airing season arrives
    week by week. Otherwise: ``imported``.
    """

    if _live_sibling(s, cand.id, download_id) is not None:
        return CandidateStatus.downloading
    target = (cand.features or {}).get("season_episodes") if cand.season is not None else None
    if target:
        covered: set[int] = set()
        pack = False
        for name in s.scalars(
            sa_select(Download.release).where(
                Download.candidate_id == cand.id, Download.state == "imported"
            )
        ).all():
            _seasons, eps = parse_season_episode(name or "")
            if not eps:
                pack = True
            covered.update(eps)
        if cand.season is not None:
            covered |= _owned_episodes(cand.title_id, cand.season)
        if not pack and len(covered) < int(target):
            return CandidateStatus.approved
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
    NN/``. A failed import records the error and leaves state ``completed`` so
    the next sync retries — the candidate is not advanced until the files are
    in place. Returns the error string on a failed import (else ``None``).
    """

    dest: str | None = None
    episodes: list[EpisodeImport] = []
    error: str | None = None

    content = st.content_path()
    if not config.torrent.import_to_library:
        log.info("import.skipped_disabled", download=download_id, title=title)
    elif content is None:
        error = "client reported no content path"
    elif kind is TitleKind.movie:
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
        except NasUnavailableError as exc:
            error = f"NAS unavailable: {redact_exc(exc)}"
            log.warning("import.nas_unavailable", download=download_id, title=title, detail=error)
        except Exception as exc:
            error = f"import failed: {redact_exc(exc)}"
            log.warning("import.failed", download=download_id, title=title, detail=error)
    else:
        try:
            from .importer import build_library_target, import_completed_episodes

            target = build_library_target(config)
            _set_download(download_id, state="importing", progress=0.0)
            episodes = await asyncio.to_thread(
                import_completed_episodes,
                config,
                target,
                content_path=content,
                series_title=title,
                season=season,
                on_progress=_import_progress_cb(download_id),
            )
            dest = os.path.dirname(episodes[-1].dest) if episodes else None
        except NasUnavailableError as exc:
            error = f"NAS unavailable: {redact_exc(exc)}"
            log.warning("import.nas_unavailable", download=download_id, title=title, detail=error)
        except Exception as exc:
            error = f"import failed: {redact_exc(exc)}"
            log.warning("import.failed", download=download_id, title=title, detail=error)

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
                cand.status = _status_after_finished(s, cand, dl.id)
    stats.completed += 1

    # Complete the pipeline: catalog the file(s) (so they're "owned") and fetch
    # subtitles now, instead of waiting for a NAS rescan + a full sweep. Best-effort
    # — a failure here never un-imports the media.
    try:
        owned_ids: list[int] = []
        if kind is TitleKind.movie and dest is not None:
            owned_ids.append(register_owned_movie(config, title_id, dest, st.name or title))
        else:
            owned_ids.extend(register_owned_episode(config, title_id, ep) for ep in episodes)
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
