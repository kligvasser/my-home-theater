"""Storage planner: how full is the NAS, how fast is it filling, what could go.

Everything here reads; nothing here deletes. Suggestions are for a person to act on.

Capacity comes from the mount, which can hang. A reading is cached, a slow one is
abandoned after a few seconds, and only one is ever in flight: each abandoned read
keeps a worker thread until the kernel lets go, and those threads are shared with
every ``to_thread`` in the app. The mount table is checked first, because an empty
folder left under ``/Volumes`` would otherwise report the boot disk.

Growth prefers measured history: one note of the NAS's usage per day, in the
``setting`` table. Until there's a week of that it falls back to what the catalog
added, which can't see deletions.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from .config import AppConfig
from .dashboard.queries import _res_rank
from .db.base import utcnow
from .db.models import (
    LIVE_DOWNLOAD_STATES,
    STOPPED_STATUSES,
    Candidate,
    CandidateStatus,
    Download,
    OwnedFile,
    Setting,
    Title,
    TitleKind,
)
from .db.session import session_scope
from .errors import redact_exc
from .health.checks import _library_dirs, _mount_points
from .logging_setup import get_logger

log = get_logger(__name__)

CAPACITY_TIMEOUT_S = 8.0
CAPACITY_TTL_S = 300.0
CAPACITY_RETRY_S = 60.0

HISTORY_KEY = "storage_history"
HISTORY_KEEP_DAYS = 400
GROWTH_WINDOW_DAYS = 90
GROWTH_MIN_DAYS = 7

LOW_RATING = 6.0
TOP_TITLES = 25
MAX_SUGGESTIONS = 20
_FILES_SHOWN = 5

# Why a title is suggested, best reason first.
TIER_WATCHED, TIER_LOW_RATING, TIER_DUPLICATE = 0, 1, 2

_PATH_SEP_RE = re.compile(r"[\\/]")
# One film split across files, not two copies of it.
_PART_RE = re.compile(r"(?i)(?:^|[\W_])(?:cd|dis[ck]|part|pt)[\W_]*\d")
# Wanted by the pipeline, so not something to suggest removing.
_LIVE_CANDIDATE = (
    CandidateStatus.approved,
    CandidateStatus.queued,
    CandidateStatus.downloading,
)


@dataclass(frozen=True, slots=True)
class Capacity:
    total: int | None = None
    used: int | None = None
    free: int | None = None
    measured_at: str | None = None
    stale: bool = False  # the last good reading, shown because a new one failed
    note: str = ""

    @property
    def used_percent(self) -> int | None:
        return round(self.used * 100 / self.total) if self.total and self.used is not None else None


@dataclass(frozen=True, slots=True)
class RootUsage:
    kind: str
    root: str
    files: int
    bytes: int
    unsized: int  # files whose size isn't known, so ``bytes`` is a floor


@dataclass(frozen=True, slots=True)
class TitleUsage:
    title: str
    kind: str
    year: int | None
    files: int
    bytes: int


@dataclass(frozen=True, slots=True)
class Growth:
    bytes_per_day: float | None
    source: str  # nas | catalog | none
    days: int  # how much history that is


@dataclass(frozen=True, slots=True)
class InFlight:
    downloads: int
    bytes: int
    estimated: int  # how many of those are guessed from a typical file


@dataclass(frozen=True, slots=True)
class Suggestion:
    title: str
    kind: str
    year: int | None
    tier: int
    reasons: tuple[str, ...]
    bytes: int
    files: tuple[str, ...] = ()  # the copies to drop; empty = the whole title


@dataclass(slots=True)
class StoragePlan:
    capacity: Capacity
    roots: list[RootUsage]
    titles: list[TitleUsage]
    growth: Growth
    in_flight: InFlight
    days_to_full: int | None
    suggestions: list[Suggestion]
    reclaimable: int
    watch_data: bool  # whether "watched" could be judged at all
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Reading:
    at: float
    capacity: Capacity | None
    note: str = ""


_cache: dict[str, _Reading] = {}
_last_good: dict[str, Capacity] = {}
_busy: set[str] = set()
_busy_lock = threading.Lock()


def clear_cache() -> None:
    _cache.clear()
    _last_good.clear()


def _disk_usage(path: str) -> tuple[int, int, int]:
    usage = shutil.disk_usage(path)
    return usage.total, usage.used, usage.free


async def _mount_problem(path: str) -> str:
    """Why ``path`` mustn't be measured, if so. Only ``/Volumes`` paths need the share
    mounted; any other library dir is a plain folder."""

    if not path.startswith("/Volumes/"):
        return ""
    try:
        mounts = await _mount_points()
    except OSError:
        mounts = None
    if mounts is None:
        return "Couldn't read the mount table."
    trimmed = path.rstrip("/")
    for mount in mounts:
        point = mount.rstrip("/")
        if point and (trimmed == point or trimmed.startswith(point + "/")):
            return ""
    return "The NAS isn't mounted."


async def _measure(path: str) -> tuple[tuple[int, int, int] | None, str]:
    with _busy_lock:
        if path in _busy:  # never a second worker on a mount that's stuck
            return None, "An earlier read of the NAS hasn't come back yet."
        _busy.add(path)

    def work() -> tuple[int, int, int]:
        try:
            return _disk_usage(path)
        finally:
            with _busy_lock:
                _busy.discard(path)

    task = asyncio.ensure_future(asyncio.to_thread(work))
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    try:
        return await asyncio.wait_for(asyncio.shield(task), CAPACITY_TIMEOUT_S), ""
    except TimeoutError:
        return None, "The NAS didn't answer in time."
    except OSError as exc:
        return None, f"Couldn't read the NAS: {redact_exc(exc)}"


async def capacity(config: AppConfig) -> Capacity:
    """The NAS's size and free space, as fresh as the cache allows. Never raises and
    never takes much longer than :data:`CAPACITY_TIMEOUT_S`."""

    dirs = _library_dirs(config)
    if not dirs:
        return Capacity(note="No local NAS mount is configured, so free space can't be read.")
    path = dirs[0]
    now = time.time()  # wall clock: a cached reading mustn't survive a night's sleep
    hit = _cache.get(path)
    if hit is not None and now - hit.at < (CAPACITY_TTL_S if hit.capacity else CAPACITY_RETRY_S):
        return hit.capacity or _fallback(path, hit.note)

    problem = await _mount_problem(path)
    usage = None
    if not problem:
        usage, problem = await _measure(path)
    if usage is None:
        _cache[path] = _Reading(now, None, problem)
        return _fallback(path, problem)
    total, used, free = usage
    reading = Capacity(total, used, free, utcnow().isoformat())
    _cache[path] = _Reading(now, reading)
    _last_good[path] = reading
    return reading


def _fallback(path: str, note: str) -> Capacity:
    last = _last_good.get(path)
    if last is None:
        return Capacity(note=note)
    return Capacity(last.total, last.used, last.free, last.measured_at, True, note)


def _load_history() -> list[dict[str, Any]]:
    with session_scope() as s:
        row = s.get(Setting, HISTORY_KEY)
        return list(json.loads(row.value)) if row is not None and row.value else []


def _append_history(day: date, reading: Capacity) -> bool:
    with session_scope() as s:
        row = s.get(Setting, HISTORY_KEY)
        if row is None:
            row = Setting(key=HISTORY_KEY)
            s.add(row)
        history = json.loads(row.value) if row.value else []
        stamp = day.isoformat()
        if history and history[-1]["day"] >= stamp:
            return False
        history.append({"day": stamp, "used": reading.used, "total": reading.total})
        row.value = json.dumps(history[-HISTORY_KEEP_DAYS:])
    return True


async def record_snapshot(config: AppConfig) -> bool:
    """Note today's usage, once. False only when the NAS can't be measured now."""

    reading = await capacity(config)
    if reading.used is None or reading.stale:
        return False
    await asyncio.to_thread(_append_history, datetime.now().date(), reading)
    return True


def _usage(config: AppConfig, s: Session) -> tuple[list[RootUsage], list[TitleUsage]]:
    names = {TitleKind.movie: config.nas.movies_root, TitleKind.series: config.nas.tv_root}
    rows = s.execute(
        select(
            OwnedFile.kind,
            func.count(),
            func.coalesce(func.sum(OwnedFile.size_bytes), 0),
            func.count(OwnedFile.size_bytes),
        ).group_by(OwnedFile.kind)
    ).all()
    roots = [
        RootUsage(kind.value, names[kind], files, int(size), files - sized)
        for kind, files, size, sized in rows
    ]
    size = func.coalesce(func.sum(OwnedFile.size_bytes), 0)
    top = s.execute(
        select(Title.title, Title.kind, Title.year, func.count(OwnedFile.id), size)
        .join(OwnedFile, OwnedFile.title_id == Title.id)
        .group_by(Title.id)
        .order_by(size.desc())
        .limit(TOP_TITLES)
    ).all()
    titles = [TitleUsage(t, k.value, y, n, int(b)) for t, k, y, n, b in top]
    return roots, titles


def _catalog_growth(s: Session, now: datetime) -> Growth:
    """Bytes the catalog gained per day. The very first scan lists the whole library
    in a day, which is no growth at all, so that day never counts."""

    first = s.scalar(select(func.min(OwnedFile.created_at)))
    if first is None:
        return Growth(None, "none", 0)
    bootstrap_end = first.date() + timedelta(days=1)
    start = max(now.date() - timedelta(days=GROWTH_WINDOW_DAYS), bootstrap_end)
    days = (now.date() - start).days
    if days < GROWTH_MIN_DAYS:
        return Growth(None, "none", max(days, 0))
    added = s.scalar(
        select(func.coalesce(func.sum(OwnedFile.size_bytes), 0)).where(
            OwnedFile.created_at >= datetime(start.year, start.month, start.day, tzinfo=now.tzinfo)
        )
    )
    return Growth(int(added or 0) / days, "catalog", days)


def _growth(history: list[dict[str, Any]], s: Session, now: datetime) -> Growth:
    cutoff = (now.date() - timedelta(days=GROWTH_WINDOW_DAYS)).isoformat()
    recent = [h for h in history if h["day"] >= cutoff]
    if len(recent) >= 2:
        days = (date.fromisoformat(recent[-1]["day"]) - date.fromisoformat(recent[0]["day"])).days
        if days >= GROWTH_MIN_DAYS:
            return Growth((recent[-1]["used"] - recent[0]["used"]) / days, "nas", days)
    return _catalog_growth(s, now)


def _watched_titles(s: Session) -> tuple[set[int], bool]:
    """Titles fully watched, and whether any watch data exists.

    Reads ``watch_state`` (one row per owned file) by name, so a database that hasn't
    got the table yet just has no opinion. A movie is watched if any copy was; a
    series only once every episode was.
    """

    bind = s.connection()
    if not inspect(bind).has_table("watch_state"):
        return set(), False
    played = {
        row[0]
        for row in s.execute(text("SELECT owned_file_id FROM watch_state WHERE play_count > 0"))
    }
    if not played:
        return set(), False
    episodes: dict[int, dict[tuple[Any, ...], bool]] = defaultdict(dict)
    for file_id, title_id, kind, season, episode in s.execute(
        select(
            OwnedFile.id, OwnedFile.title_id, OwnedFile.kind, OwnedFile.season, OwnedFile.episode
        )
    ):
        key = (season, episode) if kind is TitleKind.series else ()
        slot = episodes[title_id]
        slot[key] = slot.get(key, False) or file_id in played
    return {tid for tid, seen in episodes.items() if all(seen.values())}, True


def _base_name(path: str) -> str:
    return _PATH_SEP_RE.split(path)[-1]


def _spare_copies(files: list[Any]) -> list[Any]:
    """The copies a title could lose. The best of each episode (or movie) stays:
    highest resolution, then whichever has subtitles, then the bigger file. Parts of
    one film and overlapping multi-episode files aren't duplicates."""

    groups: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    for f in files:
        if f.kind is TitleKind.series and f.episode is None:
            continue
        groups[(f.season, f.episode, f.episode_end)].append(f)
    spare: list[Any] = []
    for copies in groups.values():
        if len(copies) < 2 or any(_PART_RE.search(_base_name(c.path)) for c in copies):
            continue
        ranked = sorted(
            copies,
            key=lambda c: (
                _res_rank([c.resolution] if c.resolution else []) or 0,
                bool(c.subtitle_langs),
                c.size_bytes or 0,
            ),
        )
        spare.extend(ranked[:-1])
    return spare


def _suggest(s: Session) -> tuple[list[Suggestion], int, bool]:
    """Ranked suggestions, what they'd free in all, and whether watch data exists."""

    watched, has_watch_data = _watched_titles(s)
    protected = set(
        s.scalars(select(Candidate.title_id).where(Candidate.status.in_(_LIVE_CANDIDATE)))
    )
    protected |= set(
        s.scalars(
            select(Candidate.title_id)
            .join(Download, Download.candidate_id == Candidate.id)
            .where(
                Download.state.in_(LIVE_DOWNLOAD_STATES),
                Candidate.status.not_in(STOPPED_STATUSES),
            )
        )
    )
    protected |= set(s.scalars(select(Title.id).where(Title.followed.is_(True))))

    by_title: dict[int, list[OwnedFile]] = defaultdict(list)
    for owned in s.scalars(select(OwnedFile)):
        by_title[owned.title_id].append(owned)

    out: list[Suggestion] = []
    titles = s.scalars(select(Title).where(Title.id.in_(list(by_title))))
    for title in titles:
        if title.id in protected:
            continue
        files = by_title[title.id]
        spare = _spare_copies(files)
        rating = title.imdb_rating if title.imdb_rating is not None else title.tmdb_rating
        low = rating is not None and rating < LOW_RATING
        reasons: list[str] = []
        if title.id in watched:
            reasons.append("watched")
        if low:
            reasons.append(f"rated {rating:.1f}")
        if spare:
            reasons.append(f"{len(spare)} duplicate cop{'y' if len(spare) == 1 else 'ies'}")
        if not reasons:
            continue
        kind, year = title.kind.value, title.year
        if title.id not in watched and not low:
            names = sorted(_base_name(f.path) for f in spare)
            shown = names[:_FILES_SHOWN]
            if len(names) > len(shown):
                shown.append(f"…and {len(names) - len(shown)} more")
            bytes_ = sum(f.size_bytes or 0 for f in spare)
            out.append(
                Suggestion(
                    title.title, kind, year, TIER_DUPLICATE, tuple(reasons), bytes_, tuple(shown)
                )
            )
            continue
        tier = TIER_WATCHED if title.id in watched else TIER_LOW_RATING
        bytes_ = sum(f.size_bytes or 0 for f in files)
        out.append(Suggestion(title.title, kind, year, tier, tuple(reasons), bytes_))
    out.sort(key=lambda x: (x.tier, -x.bytes, x.title))
    return out[:MAX_SUGGESTIONS], sum(x.bytes for x in out), has_watch_data


async def _in_flight(config: AppConfig) -> InFlight:
    """What grabs under way will take on the NAS: each one's full size, not what's
    left to download, because it all gets copied there. Sizes come from the client
    where it knows; otherwise a typical file of that kind stands in."""

    def rows() -> tuple[list[tuple[str | None, TitleKind]], dict[TitleKind, float]]:
        with session_scope() as s:
            live = s.execute(
                select(Download.external_id, Title.kind)
                .join(Candidate, Candidate.id == Download.candidate_id)
                .join(Title, Title.id == Candidate.title_id)
                .where(
                    Download.state.in_(LIVE_DOWNLOAD_STATES),
                    Candidate.status.not_in(STOPPED_STATUSES),
                )
            ).all()
            typical = s.execute(
                select(OwnedFile.kind, func.avg(OwnedFile.size_bytes)).group_by(OwnedFile.kind)
            ).all()
        return [(h, k) for h, k in live], {k: float(v or 0) for k, v in typical}

    downloads, typical = await asyncio.to_thread(rows)
    if not downloads:
        return InFlight(0, 0, 0)
    live: dict[str, Any] = {}
    hashes = frozenset(h for h, _ in downloads if h)
    if config.acquisition.backend == "torrent" and hashes:
        from .pipeline import _live_poller

        try:
            live, _ = await _live_poller(config).poll(hashes)
        except Exception as exc:  # estimates will do
            log.debug("storage.live_unavailable", detail=redact_exc(exc))
    total, estimated = 0, 0
    for infohash, kind in downloads:
        status = live.get(infohash or "")
        left = getattr(status, "left_bytes", None)
        progress = getattr(status, "progress", 1.0)
        if left is not None and progress < 1.0:
            total += int(left / (1.0 - progress))
        else:
            total += int(typical.get(kind, 0))
            estimated += 1
    return InFlight(len(downloads), total, estimated)


async def storage_plan(config: AppConfig) -> StoragePlan:
    reading = await capacity(config)
    in_flight = await _in_flight(config)
    if reading.used is not None and not reading.stale:
        await asyncio.to_thread(_append_history, datetime.now().date(), reading)

    def read() -> tuple[Any, ...]:
        with session_scope() as s:
            roots, titles = _usage(config, s)
            growth = _growth(_load_history(), s, utcnow())
            return roots, titles, growth, *_suggest(s)

    roots, titles, growth, suggestions, reclaimable, watch_data = await asyncio.to_thread(read)
    days_to_full = None
    if reading.free is not None and growth.bytes_per_day and growth.bytes_per_day > 0:
        days_to_full = int(max(reading.free - in_flight.bytes, 0) / growth.bytes_per_day)
    notes = [reading.note] if reading.note else []
    return StoragePlan(
        capacity=reading,
        roots=roots,
        titles=titles,
        growth=growth,
        in_flight=in_flight,
        days_to_full=days_to_full,
        suggestions=suggestions,
        reclaimable=reclaimable,
        watch_data=watch_data,
        notes=notes,
    )
