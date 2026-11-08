"""Pluggable candidate sources (plan §5.4).

Each source yields TMDb title stubs; the service enriches, filters, and ranks
them. The Trakt watchlist is special: items you explicitly picked bypass the
rating/vote thresholds (``skip_filter``) and are recorded with
``source=watchlist``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import Discovery, Secrets
from ..db.models import CandidateSource as CandidateOrigin
from ..db.models import TitleKind
from ..errors import NotConfiguredError, redact_exc
from ..logging_setup import get_logger
from ..metadata.dto import TmdbSeason, TmdbTitle
from ..metadata.tmdb import TMDbClient

log = get_logger(__name__)

NEW_SEASONS_TTL_DAYS = 1


@dataclass(frozen=True, slots=True)
class Discovered:
    tmdb: TmdbTitle
    kind: TitleKind
    source: str
    origin: CandidateOrigin = CandidateOrigin.discovery
    skip_filter: bool = False  # watchlist/new season: the human already chose it
    season: int | None = None  # season-scoped (series you own): grab this season
    season_episodes: int | None = None  # announced episode count of that season
    reason: str | None = None  # overrides the default skip_filter reason
    auto_grab: bool = False  # followed series: approve on creation so it grabs itself
    top_up: bool = False  # newest owned season, partly on disk: fetch the rest


class CandidateSource(Protocol):
    @property
    def name(self) -> str: ...

    async def fetch(self, client: TMDbClient, limit: int) -> list[Discovered]: ...


@dataclass
class TmdbTrendingSource:
    kind: TitleKind
    window: str = "week"

    @property
    def name(self) -> str:
        return f"trending {self.kind.value} ({self.window})"

    async def fetch(self, client: TMDbClient, limit: int) -> list[Discovered]:
        items = await client.trending(self.kind, self.window, limit=limit)
        return [Discovered(t, self.kind, self.name) for t in items]


@dataclass
class TmdbTopRatedSource:
    kind: TitleKind

    @property
    def name(self) -> str:
        return f"top-rated {self.kind.value}"

    async def fetch(self, client: TMDbClient, limit: int) -> list[Discovered]:
        items = await client.top_rated(self.kind, limit=limit)
        return [Discovered(t, self.kind, self.name) for t in items]


@dataclass
class TraktWatchlistSource:
    """Your Trakt watchlist (movies + shows). Requires `home-theater trakt-auth`."""

    client_id: str
    client_secret: str

    @property
    def name(self) -> str:
        return "trakt watchlist"

    async def fetch(self, client: TMDbClient, limit: int) -> list[Discovered]:
        from ..trakt import TraktClient

        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                items = await TraktClient(self.client_id, self.client_secret, http).watchlist()
        except NotConfiguredError as exc:
            log.info("watchlist.skipped", detail=redact_exc(exc))
            return []
        except httpx.HTTPError as exc:
            log.warning("watchlist.failed", error=redact_exc(exc))
            return []

        out: list[Discovered] = []
        for item in items:
            if item.tmdb_id is None:
                log.info("watchlist.no_tmdb_id", title=item.title)
                continue
            stub = TmdbTitle(
                tmdb_id=item.tmdb_id, title=item.title, year=item.year, imdb_id=item.imdb_id
            )
            out.append(
                Discovered(
                    stub,
                    item.kind,
                    self.name,
                    origin=CandidateOrigin.watchlist,
                    skip_filter=True,
                )
            )
        return out  # a watchlist is small and hand-picked: no limit applied


def _aired_episodes(details: TmdbTitle, season: TmdbSeason) -> int:
    """Episodes of ``season`` out so far. TMDb's episode_count includes announced
    ones, and a season can't be topped up with episodes that don't exist yet."""

    count = season.episode_count or 0
    aired_to, aired_ep = details.last_aired_season, details.last_aired_episode
    if aired_to is None or aired_ep is None or season.number < aired_to:
        return count
    if season.number > aired_to:
        return 0
    return min(count, aired_ep) if count else aired_ep


def _locate_episode(details: TmdbTitle, episode: int) -> tuple[int, int]:
    """(season, episode) for a file with no season number: a mini-series, or an
    anime numbered from 1 across seasons."""

    seen = 0
    numbered = [s for s in details.seasons if s.number > 0]
    for season in numbered:
        count = season.episode_count or 0
        if count and episode <= seen + count:
            return season.number, episode - seen
        seen += count
    return (numbered[-1].number if numbered else 1), episode - seen


@dataclass
class LibraryNewSeasonsSource:
    """New seasons of series you own *or follow*.

    Walks owned series and followed series, asks TMDb for each show's season list,
    and yields one season-scoped ``Discovered`` per aired season newer than the
    newest one on disk, plus that newest season itself if episodes have aired that
    you don't have (``top_up``). ``skip_filter``: you already chose the show, so no
    rating/vote gate. Followed series are ``auto_grab`` — their new-season
    candidates are approved on creation so they download without a manual OK; a
    followed show you own nothing of yet backfills every aired season.

    Details go through a 1-day cache, not the metadata default: a new season
    mustn't wait two weeks for a stale season list to expire.
    """

    @property
    def name(self) -> str:
        return "library new seasons"

    async def fetch(self, client: TMDbClient, limit: int) -> list[Discovered]:
        from sqlalchemy import select

        from ..db.base import utcnow
        from ..db.models import Candidate, CandidateStatus, OwnedFile, Title
        from ..db.session import session_scope
        from .service import BLOCKING_STATUSES, IN_FLIGHT, retry_due  # service imports us

        owned: dict[int, dict[int, set[int]]] = {}  # tmdb_id -> season -> episodes
        unnumbered: dict[int, list[tuple[int, int]]] = {}  # episode ranges, no season
        names: dict[int, str] = {}
        followed: set[int] = set()
        with session_scope() as s:
            for tmdb_id, name, season, episode, end in s.execute(
                select(
                    Title.tmdb_id,
                    Title.title,
                    OwnedFile.season,
                    OwnedFile.episode,
                    OwnedFile.episode_end,
                )
                .join(OwnedFile, OwnedFile.title_id == Title.id)
                .where(
                    Title.kind == TitleKind.series,
                    Title.tmdb_id.is_not(None),
                    OwnedFile.season.is_not(None) | OwnedFile.episode.is_not(None),
                )
            ).all():
                names[tmdb_id] = name
                if season is None:
                    unnumbered.setdefault(tmdb_id, []).append((episode, end or episode))
                    continue
                numbers = owned.setdefault(tmdb_id, {}).setdefault(season, set())
                if episode is not None:
                    numbers.update(range(episode, (end or episode) + 1))
            # Followed series (may own nothing yet → no seasons on disk).
            for tmdb_id, name in s.execute(
                select(Title.tmdb_id, Title.title).where(
                    Title.kind == TitleKind.series,
                    Title.tmdb_id.is_not(None),
                    Title.followed.is_(True),
                )
            ).all():
                followed.add(tmdb_id)
                owned.setdefault(tmdb_id, {})
                names[tmdb_id] = name
            for tmdb_id in unnumbered:
                owned.setdefault(tmdb_id, {})

            # Seasons already suggested (live or rejected) must not be re-emitted:
            # they'd only be dropped at persist time while eating the source limit.
            # Imported seasons are kept apart, with the episode count they covered.
            taken: set[tuple[int, int]] = set()
            imported: dict[tuple[int, int], int] = {}
            for tmdb_id, season, status, feats in s.execute(
                select(Title.tmdb_id, Candidate.season, Candidate.status, Candidate.features)
                .join(Candidate, Candidate.title_id == Title.id)
                .where(
                    Title.kind == TitleKind.series,
                    Candidate.season.is_not(None),
                    Candidate.status.in_(BLOCKING_STATUSES),
                )
            ).all():
                if status is CandidateStatus.imported:
                    covered = int((feats or {}).get("season_episodes") or 0)
                    key = (tmdb_id, season)
                    imported[key] = max(imported.get(key, 0), covered)
                else:
                    taken.add((tmdb_id, season))
            # Failed seasons wait out their backoff, judged by the newest row.
            latest_failed: dict[tuple[int, int], Candidate] = {}
            for tmdb_id, failed in s.execute(
                select(Title.tmdb_id, Candidate)
                .join(Candidate, Candidate.title_id == Title.id)
                .where(
                    Title.kind == TitleKind.series,
                    Candidate.season.is_not(None),
                    Candidate.status == CandidateStatus.failed,
                )
                .order_by(Candidate.id.desc())
            ).all():
                latest_failed.setdefault((tmdb_id, failed.season), failed)
            taken |= {key for key, row in latest_failed.items() if not retry_due(row)}
            # A whole-show candidate still in flight will cover every season.
            whole_show = set(
                s.scalars(
                    select(Title.tmdb_id)
                    .join(Candidate, Candidate.title_id == Title.id)
                    .where(
                        Title.kind == TitleKind.series,
                        Candidate.season.is_(None),
                        Candidate.status.in_(IN_FLIGHT),
                    )
                ).all()
            )

        today = utcnow().date().isoformat()
        out: list[Discovered] = []
        # Followed shows first so the auto-grab items aren't crowded out by the
        # per-run limit.
        for tmdb_id in sorted(owned, key=lambda t: (t not in followed, t)):
            if len(out) >= limit:
                log.info("new_seasons.limit_reached", limit=limit)
                break
            if tmdb_id in whole_show:
                continue
            try:
                details = await client.details(
                    tmdb_id, TitleKind.series, ttl_days=NEW_SEASONS_TTL_DAYS
                )
            except Exception as exc:  # one unresolvable show must not sink the rest
                log.warning(
                    "new_seasons.details_failed", title=names[tmdb_id], error=redact_exc(exc)
                )
                continue
            on_disk = owned[tmdb_id]
            for first, last in unnumbered.get(tmdb_id, ()):
                for number in range(first, last + 1):
                    season_no, episode = _locate_episode(details, number)
                    on_disk.setdefault(season_no, set()).add(episode)
            have = max(on_disk, default=0)
            is_followed = tmdb_id in followed
            for season in details.seasons:
                # Specials (S0) are noise; unaired seasons can't be grabbed yet.
                # ISO dates compare correctly as strings.
                if season.number < have or season.number == 0:
                    continue
                if (tmdb_id, season.number) in taken:
                    continue
                if not season.air_date or season.air_date > today:
                    continue
                key = (tmdb_id, season.number)
                top_up = season.number == have
                if top_up:
                    aired = _aired_episodes(details, season)
                    got = len(on_disk.get(season.number, ()))
                    # An import that already covered this many episodes was as
                    # complete as it could get: don't chase it round and round.
                    if got >= aired or imported.get(key, 0) >= aired:
                        continue
                    summary = f"S{season.number:02d}: you have {got} of {aired} episodes"
                elif key in imported:
                    continue
                else:
                    episodes = f" ({season.episode_count} episodes)" if season.episode_count else ""
                    summary = f"new season S{season.number:02d}{episodes}"
                if is_followed:
                    reason = f"following: {summary} (auto-grab)"
                elif top_up:
                    reason = summary
                else:
                    reason = f"{summary} — you own up to S{have:02d}"
                out.append(
                    Discovered(
                        details,
                        TitleKind.series,
                        self.name,
                        skip_filter=True,
                        season=season.number,
                        season_episodes=season.episode_count,
                        reason=reason,
                        auto_grab=is_followed,
                        top_up=top_up,
                    )
                )
        return out[:limit]


def build_sources(config: Discovery, secrets: Secrets | None = None) -> list[CandidateSource]:
    """Instantiate the enabled sources for the configured kinds."""

    kinds: list[TitleKind] = []
    if config.include_movies:
        kinds.append(TitleKind.movie)
    if config.include_series:
        kinds.append(TitleKind.series)

    sources: list[CandidateSource] = []
    for kind in kinds:
        if config.trending:
            sources.append(TmdbTrendingSource(kind, config.trending_window))
        if config.top_rated:
            sources.append(TmdbTopRatedSource(kind))
    if config.new_seasons and config.include_series:
        sources.append(LibraryNewSeasonsSource())
    if (
        config.watchlist
        and secrets is not None
        and secrets.trakt_client_id
        and secrets.trakt_client_secret
    ):
        sources.append(
            TraktWatchlistSource(
                secrets.trakt_client_id, secrets.trakt_client_secret.get_secret_value()
            )
        )
    return sources
