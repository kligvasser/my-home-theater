"""Pluggable candidate sources (plan §5.4).

Each source yields TMDb title stubs; the service enriches, filters, and ranks
them. New sources (Trakt watchlist, manual lists) implement the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import Discovery
from ..db.models import TitleKind
from ..metadata.dto import TmdbTitle
from ..metadata.tmdb import TMDbClient


@dataclass(frozen=True, slots=True)
class Discovered:
    tmdb: TmdbTitle
    kind: TitleKind
    source: str


class CandidateSource(Protocol):
    kind: TitleKind

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


def build_sources(config: Discovery) -> list[CandidateSource]:
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
    return sources
