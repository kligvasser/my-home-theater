"""Normalized data-transfer objects returned by metadata providers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TmdbTitle:
    """TMDb details normalized to our catalog's shape."""

    tmdb_id: int
    title: str
    imdb_id: str | None = None
    year: int | None = None
    runtime: int | None = None
    genres: list[str] = field(default_factory=list)
    tmdb_rating: float | None = None
    tmdb_votes: int | None = None
    popularity: float | None = None
    poster_url: str | None = None
    overview: str | None = None


@dataclass(frozen=True, slots=True)
class OmdbRatings:
    """The two fields we want from OMDb for the 'high rank + enough views' filter."""

    imdb_rating: float | None = None
    imdb_votes: int | None = None
