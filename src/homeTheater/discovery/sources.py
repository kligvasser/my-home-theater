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
from ..metadata.dto import TmdbTitle
from ..metadata.tmdb import TMDbClient

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Discovered:
    tmdb: TmdbTitle
    kind: TitleKind
    source: str
    origin: CandidateOrigin = CandidateOrigin.discovery
    skip_filter: bool = False  # watchlist/new season: the human already chose it
    season: int | None = None  # season-scoped (series you own): grab this season
    reason: str | None = None  # overrides the default skip_filter reason


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
            log.info("watchlist.skipped", detail=str(exc))
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


@dataclass
class LibraryNewSeasonsSource:
    """New seasons of series you already own.

    Walks the owned catalog (series with season-numbered files), asks TMDb for
    each show's season list, and yields one season-scoped ``Discovered`` per
    aired season newer than the newest one on disk. ``skip_filter``: you
    already chose the show, so no rating/vote gate.
    """

    @property
    def name(self) -> str:
        return "library new seasons"

    async def fetch(self, client: TMDbClient, limit: int) -> list[Discovered]:
        from sqlalchemy import select

        from ..db.base import utcnow
        from ..db.models import Candidate, OwnedFile, Title
        from ..db.session import session_scope
        from .service import BLOCKING_STATUSES  # deferred: service imports this module

        with session_scope() as s:
            rows = s.execute(
                select(Title.tmdb_id, Title.title, OwnedFile.season)
                .join(OwnedFile, OwnedFile.title_id == Title.id)
                .where(
                    Title.kind == TitleKind.series,
                    Title.tmdb_id.is_not(None),
                    OwnedFile.season.is_not(None),
                )
            ).all()
            # Seasons already suggested (live or rejected) must not be re-emitted:
            # they'd only be dropped at persist time while eating the source limit.
            taken = {
                (tmdb_id, season)
                for tmdb_id, season in s.execute(
                    select(Title.tmdb_id, Candidate.season)
                    .join(Candidate, Candidate.title_id == Title.id)
                    .where(
                        Title.kind == TitleKind.series,
                        Candidate.season.is_not(None),
                        Candidate.status.in_(BLOCKING_STATUSES),
                    )
                ).all()
            }
        latest_owned: dict[int, int] = {}  # tmdb_id -> newest season on disk
        names: dict[int, str] = {}
        for tmdb_id, name, season in rows:
            latest_owned[tmdb_id] = max(latest_owned.get(tmdb_id, 0), season)
            names[tmdb_id] = name

        today = utcnow().date().isoformat()
        out: list[Discovered] = []
        for tmdb_id in sorted(latest_owned):
            if len(out) >= limit:
                log.info("new_seasons.limit_reached", limit=limit)
                break
            try:
                details = await client.details(tmdb_id, TitleKind.series)
            except Exception as exc:  # one unresolvable show must not sink the rest
                log.warning(
                    "new_seasons.details_failed", title=names[tmdb_id], error=redact_exc(exc)
                )
                continue
            have = latest_owned[tmdb_id]
            for season in details.seasons:
                # Specials (S0) are noise; unaired seasons can't be grabbed yet.
                # ISO dates compare correctly as strings.
                if season.number <= have or season.number == 0:
                    continue
                if (tmdb_id, season.number) in taken:
                    continue
                if not season.air_date or season.air_date > today:
                    continue
                episodes = f" ({season.episode_count} episodes)" if season.episode_count else ""
                reason = f"new season S{season.number:02d}{episodes} — you own up to S{have:02d}"
                out.append(
                    Discovered(
                        details,
                        TitleKind.series,
                        self.name,
                        skip_filter=True,
                        season=season.number,
                        reason=reason,
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
