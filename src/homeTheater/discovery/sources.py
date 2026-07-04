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
    skip_filter: bool = False  # watchlist: the human already chose it


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
