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


MATRIX_FEATURES = {
    **MATRIX_DETAILS,
    "original_language": "en",
    "production_countries": [{"iso_3166_1": "US"}],
    "belongs_to_collection": {"id": 2344, "name": "The Matrix Collection"},
    "keywords": {"keywords": [{"id": 1, "name": "cyberpunk"}, {"id": 2, "name": "dystopia"}]},
    "credits": {
        "cast": [{"name": "Keanu Reeves"}, {"name": "Carrie-Anne Moss"}],
        "crew": [
            {"name": "Lana Wachowski", "job": "Director"},
            {"name": "Bill Pope", "job": "Director of Photography"},
        ],
    },
    "release_dates": {
        "results": [{"iso_3166_1": "US", "release_dates": [{"certification": "R", "type": 3}]}]
    },
}


@respx.mock
async def test_enrich_populates_ml_features(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title
    from homeTheater.metadata import enrich_catalog

    init_db()
    with session_scope() as s:
        s.add(Title(title="The Matrix", year=1999, kind=TitleKind.movie))

    respx.get(f"{TMDB}/search/movie").mock(return_value=httpx.Response(200, json=MATRIX_SEARCH))
    respx.get(f"{TMDB}/movie/603").mock(return_value=httpx.Response(200, json=MATRIX_FEATURES))
    respx.get(OMDB).mock(return_value=httpx.Response(200, json=MATRIX_OMDB))

    await enrich_catalog(get_config())

    with session_scope() as s:
        t = s.query(Title).one()
        assert t.original_language == "en"
        assert t.origin_countries == ["US"]
        assert t.certification == "R"
        assert t.keywords == ["cyberpunk", "dystopia"]
        assert t.cast_top == ["Keanu Reeves", "Carrie-Anne Moss"]
        assert t.directors == ["Lana Wachowski"]  # DP is not a director
        assert t.collection_tmdb_id == 2344
        assert t.release_date == "1999-03-30"
        assert t.last_enriched_at is not None

        from homeTheater.features import extract_features

        feats = extract_features(t)
        assert feats["decade"] == 1990 and feats["in_collection"] is True


@respx.mock
async def test_enrich_merges_duplicate_titles(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two catalog rows resolving to the same TMDb id merge instead of sinking
    the whole batch on the unique index."""

    _setup_env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.metadata import enrich_catalog

    init_db()
    with session_scope() as s:
        a = Title(title="The Matrix", year=1999, kind=TitleKind.movie)
        a.owned_files = [OwnedFile(path="/a.mkv", kind=TitleKind.movie)]
        b = Title(title="the matrix", year=1999, kind=TitleKind.movie)
        b.owned_files = [OwnedFile(path="/b.mkv", kind=TitleKind.movie)]
        s.add_all([a, b])

    respx.get(f"{TMDB}/search/movie").mock(return_value=httpx.Response(200, json=MATRIX_SEARCH))
    respx.get(f"{TMDB}/movie/603").mock(return_value=httpx.Response(200, json=MATRIX_DETAILS))
    respx.get(OMDB).mock(return_value=httpx.Response(200, json=MATRIX_OMDB))

    stats = await enrich_catalog(get_config())
    assert stats.merged == 1
    assert not stats.errors

    with session_scope() as s:
        t = s.query(Title).one()  # one canonical row left
        assert t.tmdb_id == 603
        assert {f.path for f in t.owned_files} == {"/a.mkv", "/b.mkv"}

    # Re-running is a no-op (nothing pending anymore).
    stats2 = await enrich_catalog(get_config())
    assert stats2.titles_considered == 0


@respx.mock
async def test_omdb_rate_limit_response_not_cached(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db
    from homeTheater.metadata.omdb import OMDbClient

    init_db()
    route = respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "False", "Error": "Request limit reached!"}
        )
    )
    async with httpx.AsyncClient() as http:
        client = OMDbClient("k", http, cache_days=14)
        await client.by_imdb_id("tt0133093")
        await client.by_imdb_id("tt0133093")
    assert route.call_count == 2  # transient error was not cached

    # ...but a definitive not-found IS cached.
    route.mock(
        return_value=httpx.Response(
            200, json={"Response": "False", "Error": "Incorrect IMDb ID. Movie not found!"}
        )
    )
    async with httpx.AsyncClient() as http:
        client = OMDbClient("k", http, cache_days=14)
        await client.by_imdb_id("tt0000001")
        await client.by_imdb_id("tt0000001")
    assert route.call_count == 3  # one live call, second served from cache


@respx.mock
async def test_omdb_failure_does_not_discard_tmdb_details(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing OMDb call must not throw away the TMDb details we already got."""

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
    respx.get(OMDB).mock(return_value=httpx.Response(401, json={"Error": "Invalid API key!"}))

    stats = await enrich_catalog(get_config())
    assert stats.details_updated == 1  # TMDb details persisted despite OMDb 401
    assert stats.ratings_updated == 0
    assert not stats.errors  # OMDb failure is non-fatal, not a title error

    with session_scope() as s:
        from sqlalchemy import select

        t = s.scalar(select(Title).where(Title.title == "The Matrix"))
        assert t.tmdb_id == 603 and t.imdb_id == "tt0133093"  # kept
        assert t.imdb_rating is None  # ratings just missing


@respx.mock
async def test_force_reenriches_recently_attempted_titles(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`force=True` backfills a title enriched moments ago (e.g. after adding OMDb)."""

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
    # First pass: OMDb unavailable -> title gets details but no rating, and a
    # fresh last_enriched_at that would normally block re-enrichment for days.
    omdb = respx.get(OMDB).mock(return_value=httpx.Response(401, json={"Error": "nope"}))
    await enrich_catalog(get_config())

    # OMDb now works; a normal run skips (recently enriched), force backfills.
    omdb.mock(return_value=httpx.Response(200, json=MATRIX_OMDB))
    assert (await enrich_catalog(get_config())).titles_considered == 0
    forced = await enrich_catalog(get_config(), force=True)
    assert forced.ratings_updated == 1

    with session_scope() as s:
        from sqlalchemy import select

        assert s.scalar(select(Title.imdb_rating).where(Title.title == "The Matrix")) == 8.7
