"""Metadata clients + enrichment flow with all HTTP mocked via respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.db.models import TitleKind

TMDB = "https://api.themoviedb.org/3"
OMDB = "https://www.omdbapi.com/"

MATRIX_SEARCH = {
    "results": [
        {"id": 999, "title": "The Matrix Reloaded", "release_date": "2003-05-15"},
        {"id": 603, "title": "The Matrix", "release_date": "1999-03-30"},
    ]
}
MATRIX_DETAILS = {
    "id": 603,
    "title": "The Matrix",
    "release_date": "1999-03-30",
    "runtime": 136,
    "vote_average": 8.2,
    "vote_count": 24000,
    "popularity": 77.7,
    "poster_path": "/matrix.jpg",
    "overview": "A hacker learns the truth.",
    "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Science Fiction"}],
    "external_ids": {"imdb_id": "tt0133093"},
    "imdb_id": "tt0133093",
}
MATRIX_OMDB = {"Response": "True", "imdbRating": "8.7", "imdbVotes": "1,900,000"}


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _setup_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-key")
    monkeypatch.setenv("OMDB_API_KEY", "omdb-key")
    _reset()


@respx.mock
async def test_tmdb_prefers_year_match(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db
    from homeTheater.metadata.tmdb import TMDbClient

    init_db()
    respx.get(f"{TMDB}/search/movie").mock(return_value=httpx.Response(200, json=MATRIX_SEARCH))

    async with httpx.AsyncClient() as http:
        client = TMDbClient("k", http, cache_days=0)
        tmdb_id = await client.search("The Matrix", 1999, TitleKind.movie)
    # 603 (1999) is picked over the top result 999 (2003) because the year matches.
    assert tmdb_id == 603


@respx.mock
async def test_details_are_cached(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db
    from homeTheater.metadata.tmdb import TMDbClient

    init_db()
    route = respx.get(f"{TMDB}/movie/603").mock(
        return_value=httpx.Response(200, json=MATRIX_DETAILS)
    )

    async with httpx.AsyncClient() as http:
        client = TMDbClient("k", http, cache_days=14)
        first = await client.details(603, TitleKind.movie)
        second = await client.details(603, TitleKind.movie)

    assert route.call_count == 1  # second call served from cache
    assert first.imdb_id == second.imdb_id == "tt0133093"
    assert first.genres == ["Action", "Science Fiction"]


@respx.mock
async def test_enrich_backfills_catalog(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title
    from homeTheater.metadata import enrich_catalog

    init_db()
    with session_scope() as s:
        s.add(Title(title="The Matrix", year=1999, kind=TitleKind.movie))

    respx.get(f"{TMDB}/search/movie").mock(return_value=httpx.Response(200, json=MATRIX_SEARCH))
    respx.get(f"{TMDB}/movie/603").mock(return_value=httpx.Response(200, json=MATRIX_DETAILS))
    respx.get(OMDB).mock(return_value=httpx.Response(200, json=MATRIX_OMDB))

    stats = await enrich_catalog(get_config())

    assert stats.ids_resolved == 1
    assert stats.ratings_updated == 1

    with session_scope() as s:
        from sqlalchemy import select

        title = s.scalar(select(Title).where(Title.title == "The Matrix"))
        assert title is not None
        assert title.tmdb_id == 603
        assert title.imdb_id == "tt0133093"
        assert title.imdb_rating == 8.7
        assert title.imdb_votes == 1_900_000  # parsed from "1,900,000"
        assert title.runtime == 136
        assert {g.name for g in title.genres} == {"Action", "Science Fiction"}
