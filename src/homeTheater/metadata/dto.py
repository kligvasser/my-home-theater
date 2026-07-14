"""Normalized data-transfer objects returned by metadata providers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TmdbSeason:
    """One season of a TV series (from the ``seasons`` array of tv details)."""

    number: int
    episode_count: int | None = None
    air_date: str | None = None  # ISO yyyy-mm-dd of the first episode


@dataclass(frozen=True, slots=True)
class TmdbTitle:
    """TMDb details normalized to our catalog's shape."""

    tmdb_id: int
    title: str
    imdb_id: str | None = None
    tvdb_id: int | None = None
    year: int | None = None
    runtime: int | None = None
    genres: list[str] = field(default_factory=list)
    tmdb_rating: float | None = None
    tmdb_votes: int | None = None
    popularity: float | None = None
    poster_url: str | None = None
    overview: str | None = None
    # Taste/ML features (see homeTheater.features). All optional: list payloads
    # (trending/top_rated) don't carry them; details() does.
    original_language: str | None = None
    origin_countries: list[str] = field(default_factory=list)
    release_date: str | None = None
    certification: str | None = None
    keywords: list[str] = field(default_factory=list)
    cast_top: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    collection_tmdb_id: int | None = None
    collection_name: str | None = None
    seasons_count: int | None = None
    episodes_count: int | None = None
    series_status: str | None = None
    seasons: list[TmdbSeason] = field(default_factory=list)  # series details only


@dataclass(frozen=True, slots=True)
class OmdbRatings:
    """The two fields we want from OMDb for the 'high rank + enough views' filter."""

    imdb_rating: float | None = None
    imdb_votes: int | None = None
