"""``watch_state``: one answer to "has this file been watched?", fed by Kodi and Trakt.

Both sources merge through :func:`merge_watch`: the newest play wins and the play
count never drops, so neither source can overwrite something fresher from the other.
Only the source that reported a play can take it back.

Trakt runs both ways. Pull comes first, so the push decides from what Trakt holds now.
A push is sent only for plays Kodi supplied, with their ``watched_at``, and only when
Trakt has no play of that item near that time. ``trakt_pushed_at`` is the local ledger;
Trakt's own list is what makes a lost reply safe to retry.

Archive suggestions are read-only: nothing here deletes, unfollows or moves anything.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .config import AppConfig
from .db.base import utcnow
from .db.models import OwnedFile, Setting, Title, TitleKind, WatchState
from .db.session import session_scope
from .errors import NotConfiguredError, redact_exc
from .locks import job_lock
from .logging_setup import get_logger

if TYPE_CHECKING:
    from .trakt import TraktClient, WatchedEpisode, WatchedMovie

log = get_logger(__name__)

SOURCE_KODI = "kodi"
SOURCE_TRAKT = "trakt"
KODI_STATUS_KEY = "kodi_sync_status"
TRAKT_STATUS_KEY = "trakt_sync_status"
TRAKT_LOCK = "trakt-sync"
TRAKT_TIMEOUT_S = 30.0
PUSH_BATCH_FILES = 100
PUSH_PAUSE_S = 1.0  # Trakt limits writes to about one a second
# Plays of one item this close are the same watch, seen twice: a Kodi add-on scrobbled
# it, or the zone in kodi.timezone changed. Pushing it would be a duplicate.
SAME_WATCH_WINDOW = timedelta(hours=24)
MAX_SUGGESTIONS = 200
_ENDED_STATUSES = frozenset({"ended", "canceled", "cancelled"})

_RemoteKey = tuple[str, int, int | None, int | None]


@dataclass
class TraktSyncStats:
    movies_seen: int = 0
    episodes_seen: int = 0
    updated: int = 0
    unmatched: int = 0  # Trakt plays of something not in the catalog
    pushed: int = 0  # files sent
    added_on_trakt: int = 0  # what Trakt said it added
    already_on_trakt: int = 0
    not_found_on_trakt: int = 0
    push_skipped: int = 0  # no play date or no ids: can't be pushed idempotently
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _PushFile:
    owned_file_id: int
    title_id: int
    kind: TitleKind
    ids: dict[str, Any]
    season: int | None
    episodes: tuple[int, ...]
    watched_at: datetime


@dataclass(frozen=True, slots=True)
class ArchiveSuggestion:
    title_id: int
    title: str
    year: int | None
    status: str
    episodes: int
    size_bytes: int
    followed: bool


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything stored is UTC."""

    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def save_status(key: str, stats: dict[str, Any]) -> None:
    with session_scope() as s:
        row = s.get(Setting, key)
        if row is None:
            row = Setting(key=key)
            s.add(row)
        row.value = json.dumps({"finished_at": utcnow().isoformat(), **stats})


def load_status(key: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.get(Setting, key)
        if row is None or not row.value:
            return None
        try:
            data = json.loads(row.value)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


def merge_watch(
    s: Session,
    owned_file_id: int,
    source: str,
    plays: int,
    played_at: datetime | None,
    now: datetime,
) -> bool:
    """Fold one source's report into the row. True if anything changed."""

    row = s.get(WatchState, owned_file_id)
    if row is None:
        if plays <= 0:
            return False
        s.add(
            WatchState(
                owned_file_id=owned_file_id,
                play_count=plays,
                last_played_at=played_at,
                source=source,
                updated_at=now,
            )
        )
        return True
    if plays <= 0:
        # Marked unwatched at the source that said it was watched.
        if row.source != source or row.play_count == 0:
            return False
        row.play_count = 0
        row.last_played_at = None
        row.updated_at = now
        return True
    current = _aware(row.last_played_at)
    newer = played_at is not None and (current is None or played_at > current)
    if not newer and plays <= row.play_count:
        return False
    if newer:
        row.last_played_at = played_at
        row.source = source
    row.play_count = max(row.play_count, plays)
    row.updated_at = now
    return True


def watched_title_ids(s: Session) -> set[int]:
    """Titles with at least one watched file. Empty before ``watch_state`` exists."""

    try:
        return set(
            s.scalars(
                select(OwnedFile.title_id)
                .join(WatchState, WatchState.owned_file_id == OwnedFile.id)
                .where(WatchState.play_count > 0)
                .distinct()
            )
        )
    except OperationalError:
        return set()


def _episode_files(s: Session) -> dict[int, list[OwnedFile]]:
    out: dict[int, list[OwnedFile]] = {}
    for f in s.scalars(select(OwnedFile).where(OwnedFile.kind == TitleKind.series)):
        out.setdefault(f.title_id, []).append(f)
    return out


def _covered(f: OwnedFile) -> range:
    assert f.episode is not None
    return range(f.episode, (f.episode_end or f.episode) + 1)


def _apply_trakt(
    movies: list[WatchedMovie], episodes: list[WatchedEpisode], stats: TraktSyncStats
) -> dict[_RemoteKey, datetime]:
    """Merge Trakt's plays; returns what Trakt holds for catalog titles, for the push."""

    now = utcnow()
    remote: dict[_RemoteKey, datetime] = {}
    with session_scope() as s:
        by_tmdb: dict[tuple[TitleKind, int], int] = {}
        by_imdb: dict[tuple[TitleKind, str], int] = {}
        for title_id, kind, tmdb_id, imdb_id in s.execute(
            select(Title.id, Title.kind, Title.tmdb_id, Title.imdb_id)
        ):
            if tmdb_id is not None:
                by_tmdb[(kind, tmdb_id)] = title_id
            if imdb_id:
                by_imdb[(kind, imdb_id)] = title_id

        def title_for(kind: TitleKind, tmdb_id: int | None, imdb_id: str | None) -> int | None:
            found = by_tmdb.get((kind, tmdb_id)) if tmdb_id is not None else None
            if found is None and imdb_id:
                found = by_imdb.get((kind, imdb_id))
            return found

        movie_files: dict[int, list[int]] = {}
        for owned_id, title_id in s.execute(
            select(OwnedFile.id, OwnedFile.title_id).where(OwnedFile.kind == TitleKind.movie)
        ):
            movie_files.setdefault(title_id, []).append(owned_id)
        for movie in movies:
            stats.movies_seen += 1
            title_id = title_for(TitleKind.movie, movie.tmdb_id, movie.imdb_id)
            if title_id is None:
                stats.unmatched += 1
                continue
            if movie.last_watched_at is not None:
                remote[("movie", title_id, None, None)] = movie.last_watched_at
            for owned_id in movie_files.get(title_id, ()):
                if merge_watch(s, owned_id, SOURCE_TRAKT, movie.plays, movie.last_watched_at, now):
                    stats.updated += 1

        played: dict[tuple[int, int, int], WatchedEpisode] = {}
        for episode in episodes:
            stats.episodes_seen += 1
            title_id = title_for(TitleKind.series, episode.show_tmdb_id, episode.show_imdb_id)
            if title_id is None:
                stats.unmatched += 1
                continue
            played[(title_id, episode.season, episode.episode)] = episode
            if episode.last_watched_at is not None:
                key = ("episode", title_id, episode.season, episode.episode)
                remote[key] = episode.last_watched_at
        for title_id, files in _episode_files(s).items():
            for f in files:
                if f.season is None or f.episode is None:
                    continue
                # A multi-episode file counts once all of it has been watched.
                seen = [played.get((title_id, f.season, e)) for e in _covered(f)]
                if not all(seen):
                    continue
                plays = min(e.plays for e in seen if e)
                dates = [e.last_watched_at for e in seen if e and e.last_watched_at]
                when = max(dates) if dates else None
                if merge_watch(s, f.id, SOURCE_TRAKT, plays, when, now):
                    stats.updated += 1
    return remote


def _trakt_ids(title: Title) -> dict[str, Any]:
    ids: dict[str, Any] = {}
    if title.tmdb_id is not None:
        ids["tmdb"] = title.tmdb_id
    if title.imdb_id:
        ids["imdb"] = title.imdb_id
    return ids


def _on_trakt(theirs: datetime | None, ours: datetime) -> bool:
    return theirs is not None and ours - theirs < SAME_WATCH_WINDOW


def _plan_push(remote: dict[_RemoteKey, datetime], stats: TraktSyncStats) -> list[_PushFile]:
    """Kodi's plays Trakt doesn't have yet. Ones it turns out to have enter the ledger."""

    plan: list[_PushFile] = []
    with session_scope() as s:
        rows = s.execute(
            select(WatchState, OwnedFile, Title)
            .join(OwnedFile, OwnedFile.id == WatchState.owned_file_id)
            .join(Title, Title.id == OwnedFile.title_id)
            .where(WatchState.source == SOURCE_KODI, WatchState.play_count > 0)
        ).all()
        for row, owned, title in rows:
            when = _aware(row.last_played_at)
            if when is None:
                stats.push_skipped += 1  # Trakt would date it "now", and again next time
                continue
            if _aware(row.trakt_pushed_at) == when:
                continue
            ids = _trakt_ids(title)
            if not ids or owned.kind is not title.kind:
                stats.push_skipped += 1
                continue
            if title.kind is TitleKind.movie:
                keys: list[_RemoteKey] = [("movie", title.id, None, None)]
                episodes: tuple[int, ...] = ()
            else:
                if owned.season is None or owned.episode is None:
                    stats.push_skipped += 1
                    continue
                episodes = tuple(_covered(owned))
                keys = [("episode", title.id, owned.season, e) for e in episodes]
                episodes = tuple(
                    e
                    for e in episodes
                    if not _on_trakt(remote.get(("episode", title.id, owned.season, e)), when)
                )
            if all(_on_trakt(remote.get(k), when) for k in keys):
                row.trakt_pushed_at = row.last_played_at
                stats.already_on_trakt += 1
                continue
            plan.append(
                _PushFile(owned.id, title.id, title.kind, ids, owned.season, episodes, when)
            )
    return plan


def _trakt_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def history_payload(files: Iterable[_PushFile]) -> dict[str, Any]:
    """Body of ``POST /sync/history``. Every entry carries its own ``watched_at``."""

    movies: list[dict[str, Any]] = []
    shows: dict[int, dict[str, Any]] = {}
    for f in files:
        watched_at = _trakt_time(f.watched_at)
        if f.kind is TitleKind.movie:
            movies.append({"ids": f.ids, "watched_at": watched_at})
            continue
        show = shows.setdefault(f.title_id, {"ids": f.ids, "seasons": {}})
        season = show["seasons"].setdefault(f.season, [])
        season.extend({"number": e, "watched_at": watched_at} for e in f.episodes)
    payload: dict[str, Any] = {}
    if movies:
        payload["movies"] = movies
    if shows:
        payload["shows"] = [
            {
                "ids": show["ids"],
                "seasons": [
                    {"number": number, "episodes": episodes}
                    for number, episodes in sorted(show["seasons"].items())
                ],
            }
            for show in shows.values()
        ]
    return payload


def _mark_pushed(files: list[_PushFile]) -> None:
    with session_scope() as s:
        for f in files:
            row = s.get(WatchState, f.owned_file_id)
            if row is not None and _aware(row.last_played_at) == f.watched_at:
                row.trakt_pushed_at = row.last_played_at


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


async def _push(
    client: TraktClient, remote: dict[_RemoteKey, datetime], stats: TraktSyncStats
) -> None:
    """Batched. A failed batch stays unmarked; the next run's pull decides again."""

    plan = await asyncio.to_thread(_plan_push, remote, stats)
    for start in range(0, len(plan), PUSH_BATCH_FILES):
        if start:
            await asyncio.sleep(PUSH_PAUSE_S)
        batch = plan[start : start + PUSH_BATCH_FILES]
        reply = await client.add_history(history_payload(batch))
        added = reply.get("added") or {}
        missing = reply.get("not_found") or {}
        stats.pushed += len(batch)
        stats.added_on_trakt += int(added.get("movies") or 0) + int(added.get("episodes") or 0)
        stats.not_found_on_trakt += sum(_count(v) for v in missing.values())
        # Not-found items are marked too: retrying can't help until the play changes.
        await asyncio.to_thread(_mark_pushed, batch)


async def sync_trakt(config: AppConfig) -> TraktSyncStats:
    """Pull Trakt's watched history, then push Kodi's if ``trakt.sync.push`` is on."""

    from .trakt import TraktClient

    if not config.trakt.sync.enabled:
        raise NotConfiguredError("trakt.sync.enabled is off.")
    client_id = config.secrets.trakt_client_id
    client_secret = config.secrets.trakt_client_secret
    if not (client_id and client_secret):
        raise NotConfiguredError("TRAKT_CLIENT_ID / TRAKT_CLIENT_SECRET are not set.")
    stats = TraktSyncStats()
    with job_lock(config, TRAKT_LOCK):
        async with httpx.AsyncClient(timeout=TRAKT_TIMEOUT_S) as http:
            client = TraktClient(client_id, client_secret.get_secret_value(), http)
            movies, episodes = await client.watched()
            remote = await asyncio.to_thread(_apply_trakt, movies, episodes, stats)
            if config.trakt.sync.push:
                try:
                    await _push(client, remote, stats)
                except (httpx.HTTPError, ValueError) as exc:
                    # The pull still counts, and the push is safe to repeat.
                    stats.errors.append(f"push stopped: {redact_exc(exc)}")
                    log.warning("trakt.push_failed", detail=redact_exc(exc))
        await asyncio.to_thread(save_status, TRAKT_STATUS_KEY, stats.as_dict())
    log.info("trakt.sync_done", updated=stats.updated, pushed=stats.pushed)
    return stats


def archive_suggestions(limit: int = MAX_SUGGESTIONS) -> list[ArchiveSuggestion]:
    """Ended series with every episode owned and watched. Suggestions only.

    Specials don't count either way. A series TMDb has no episode count for is left
    out: "fully watched" can't be known.
    """

    out: list[ArchiveSuggestion] = []
    with session_scope() as s:
        titles = s.scalars(
            select(Title).where(
                Title.kind == TitleKind.series,
                Title.series_status.is_not(None),
                Title.episodes_count.is_not(None),
            )
        ).all()
        watched = set(s.scalars(select(WatchState.owned_file_id).where(WatchState.play_count > 0)))
        files_by_title = _episode_files(s)
        for t in titles:
            assert t.series_status is not None and t.episodes_count is not None
            if t.series_status.strip().lower() not in _ENDED_STATUSES:
                continue
            files = [f for f in files_by_title.get(t.id, []) if f.season != 0]
            if not files or any(f.id not in watched for f in files):
                continue
            seen = {
                (f.season, e)
                for f in files
                if f.season is not None and f.episode is not None
                for e in _covered(f)
            }
            if len(seen) < t.episodes_count:
                continue
            out.append(
                ArchiveSuggestion(
                    title_id=t.id,
                    title=t.title,
                    year=t.year,
                    status=t.series_status,
                    episodes=len(seen),
                    size_bytes=sum(f.size_bytes or 0 for f in files),
                    followed=t.followed,
                )
            )
    out.sort(key=lambda a: a.size_bytes, reverse=True)
    return out[:limit]
