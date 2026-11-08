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
    from homeTheater.errors import TransientProviderError

    async with httpx.AsyncClient() as http:
        client = OMDbClient("k", http, cache_days=14)
        # A transient failure (quota) raises so the caller can defer, and is not
        # cached — the next attempt hits the network again.
        for _ in range(2):
            with pytest.raises(TransientProviderError):
                await client.by_imdb_id("tt0133093")
    assert route.call_count == 2

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


@respx.mock
async def test_transient_omdb_defers_last_enriched_at(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quota/outage OMDb failure must NOT stamp last_enriched_at, or the title's
    rating won't be retried for cache_days."""
    _setup_env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title, TitleKind
    from homeTheater.metadata.service import enrich_catalog

    init_db()
    with session_scope() as s:
        s.add(Title(tmdb_id=603, title="The Matrix", year=1999, kind=TitleKind.movie))

    respx.get(f"{TMDB}/movie/603").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 603,
                "title": "The Matrix",
                "release_date": "1999-03-30",
                "vote_average": 8.2,
                "vote_count": 24000,
                "genres": [{"id": 1, "name": "Action"}],
                "external_ids": {"imdb_id": "tt0133093"},
                "imdb_id": "tt0133093",
            },
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "False", "Error": "Request limit reached!"}
        )
    )

    await enrich_catalog(get_config())

    from sqlalchemy import select

    with session_scope() as s:
        t = s.scalar(select(Title).where(Title.tmdb_id == 603))
        assert t.tmdb_rating == 8.2  # TMDb details still applied
        assert t.imdb_rating is None  # ratings deferred
        assert t.last_enriched_at is None  # NOT stamped → retried next run


# --- search fallbacks and name verification (L9) ---


async def _search(title: str, year: int | None, kind: TitleKind = TitleKind.movie) -> int | None:
    from homeTheater.metadata.tmdb import TMDbClient

    async with httpx.AsyncClient() as http:
        return await TMDbClient("k", http).search(title, year, kind)


def _by_year(pages: dict[object, list[dict]], param: str = "year") -> object:
    def respond(request: httpx.Request) -> httpx.Response:
        wanted = request.url.params.get(param)
        return httpx.Response(200, json={"results": pages.get(wanted, [])})

    return respond


@respx.mock
async def test_search_retries_neighbouring_years_then_none(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    hit = {"id": 7, "title": "Severance", "first_air_date": "2022-02-18"}
    route = respx.get(f"{TMDB}/search/tv").mock(
        side_effect=_by_year({"2022": [hit]}, "first_air_date_year")
    )
    assert await _search("Severance", 2021, TitleKind.series) == 7
    assert route.call_count == 2  # 2021 empty, then 2022

    route = respx.get(f"{TMDB}/search/movie").mock(side_effect=_by_year({None: [{**hit}]}))
    assert await _search("Severance", 1990) == 7
    years = [c.request.url.params.get("year") for c in route.calls]
    assert years == ["1990", "1991", "1989", None]


@respx.mock
async def test_empty_search_is_not_remembered(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two weeks of cached "nothing" kept real titles unmatched."""

    _setup_env(monkeypatch)
    from homeTheater.db import init_db
    from homeTheater.metadata.cache import cache_get

    init_db()
    route = respx.get(f"{TMDB}/search/movie").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    assert await _search("Brand New Film", None) is None
    assert cache_get("tmdb", "search:movie:en-US:brand new film:", 14) is None

    route.mock(
        return_value=httpx.Response(200, json={"results": [{"id": 9, "title": "Brand New Film"}]})
    )
    assert await _search("Brand New Film", None) == 9


@respx.mock
async def test_previously_cached_empty_search_is_ignored(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db
    from homeTheater.metadata.cache import cache_set

    init_db()
    cache_set("tmdb", "search:movie:en-US:brand new film:", {"results": []})
    respx.get(f"{TMDB}/search/movie").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 9, "title": "Brand New Film"}]})
    )
    assert await _search("Brand New Film", None) == 9


@respx.mock
async def test_search_refuses_a_result_with_another_name(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``results[0]`` used to be accepted, so ``bkd-tk`` became some random show."""

    _setup_env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    wrong = {"id": 5, "name": "Unrelated Drama", "original_name": "Unrelated Drama"}
    respx.get(f"{TMDB}/search/tv").mock(return_value=httpx.Response(200, json={"results": [wrong]}))
    respx.get(f"{TMDB}/tv/5").mock(
        return_value=httpx.Response(200, json={"name": "Unrelated Drama"})
    )
    assert await _search("bkd-tk", None, TitleKind.series) is None


@respx.mock
async def test_search_prefers_the_nearest_year_among_same_names(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    results = [
        {"id": 1, "name": "Dark Matter", "first_air_date": "2015-06-12"},
        {"id": 2, "name": "Dark Matter", "first_air_date": "2024-05-08"},
    ]
    respx.get(f"{TMDB}/search/tv").mock(return_value=httpx.Response(200, json={"results": results}))
    assert await _search("Dark Matter", 2024, TitleKind.series) == 2


@respx.mock
async def test_original_title_is_enough(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    hit = {"id": 3, "title": "Money Heist", "original_title": "La Casa de Papel"}
    respx.get(f"{TMDB}/search/movie").mock(
        return_value=httpx.Response(200, json={"results": [hit]})
    )
    assert await _search("La Casa de Papel", None) == 3


@respx.mock
@pytest.mark.parametrize(
    ("query", "payload"),
    [
        # A Hebrew title TMDb only holds as a translation of a foreign show.
        (
            "ההתקפה על הטיטאנים",
            {
                "translations": {
                    "translations": [{"iso_639_1": "he", "data": {"name": "ההתקפה על הטיטאנים"}}]
                }
            },
        ),
        # Hebrew written with niqqud matches the plain spelling.
        (
            "שְׁטִיסֶל",
            {"translations": {"translations": [{"iso_639_1": "he", "data": {"name": "שטיסל"}}]}},
        ),
        # A romanized anime name, held as an alternative title.
        (
            "Shingeki no Kyojin",
            {"alternative_titles": {"results": [{"title": "Shingeki no Kyojin"}]}},
        ),
    ],
)
async def test_translated_and_alternative_names_keep_a_title_matchable(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, query: str, payload: dict
) -> None:
    """The similarity check must not make non-English titles unmatchable."""

    _setup_env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    hit = {"id": 1429, "name": "Attack on Titan", "original_name": "進撃の巨人"}
    respx.get(f"{TMDB}/search/tv").mock(return_value=httpx.Response(200, json={"results": [hit]}))
    names = respx.get(f"{TMDB}/tv/1429").mock(
        return_value=httpx.Response(200, json={"name": "Attack on Titan", **payload})
    )
    assert await _search(query, None, TitleKind.series) == 1429
    assert "translations" in names.calls[0].request.url.params["append_to_response"]


def test_names_match_rules() -> None:
    from homeTheater.metadata.names import names_match

    assert names_match("The Killing", ["Killing"])
    assert names_match("Better Call Soul", ["Better Call Saul"])
    assert names_match("The Reluctant Traveler", ["The Reluctant Traveller"])
    assert names_match("Dexter - Resurrection", ["Dexter: Resurrection"])
    assert names_match("Dune Part 2", ["Dune: Part Two"])
    assert names_match("Rocky II", ["Rocky 2"])
    assert names_match("Amelie", ["Amélie"])
    assert names_match("Fast & Furious", ["Fast and Furious"])
    assert names_match("Dont Look Up", ["Don't Look Up"])

    assert not names_match("Rocky 2", ["Rocky 3"])
    assert not names_match("Toy Story 2", ["Toy Story 3"])
    assert not names_match("Rocky II", ["Rocky III"])
    assert not names_match("Alien", ["Aliens"])
    assert not names_match("Silo", ["Solo"])
    assert not names_match("", ["Anything"])
    assert not names_match("!!!", ["Anything"])
    assert not names_match("Star Wars Episode IV", ["Star Wars: Episode IV - A New Hope"])
    assert names_match(
        "Star Wars Episode IV", ["Star Wars: Episode IV - A New Hope"], exact_year=True
    )


@respx.mock
async def test_mismatched_result_cannot_merge_two_titles(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's real job: a junk title must not swallow an enriched one."""

    _setup_env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.metadata import enrich_catalog

    init_db()
    with session_scope() as s:
        real = Title(title="The Matrix", year=1999, kind=TitleKind.movie, tmdb_id=603)
        real.imdb_rating = 8.7
        junk = Title(title="bkd-tk", kind=TitleKind.movie)
        junk.owned_files = [OwnedFile(path="/junk.mkv", kind=TitleKind.movie)]
        s.add_all([real, junk])

    respx.get(f"{TMDB}/search/movie").mock(return_value=httpx.Response(200, json=MATRIX_SEARCH))
    respx.get(f"{TMDB}/movie/603").mock(return_value=httpx.Response(200, json=MATRIX_DETAILS))
    respx.get(f"{TMDB}/movie/999").mock(
        return_value=httpx.Response(200, json={"title": "The Matrix Reloaded"})
    )

    stats = await enrich_catalog(get_config())
    assert stats.merged == 0 and stats.unmatched == 1
    with session_scope() as s:
        assert s.query(Title).count() == 2


# --- merging (L13) ---


@respx.mock
async def test_merge_ors_flags_and_fills_gaps(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Genre, Title
    from homeTheater.metadata import enrich_catalog

    init_db()
    with session_scope() as s:
        canonical = Title(
            title="The Matrix", year=1999, kind=TitleKind.movie, tmdb_id=603, imdb_rating=8.7
        )
        dup = Title(
            title="the matrix",
            year=1999,
            kind=TitleKind.movie,
            followed=True,
            arr_has_file=True,
            collection_name="only the duplicate",
            certification="R",
            imdb_rating=1.0,
        )
        dup.genres = [Genre(name="Cyberpunk")]
        s.add_all([canonical, dup])

    respx.get(f"{TMDB}/search/movie").mock(return_value=httpx.Response(200, json=MATRIX_SEARCH))
    details = {k: v for k, v in MATRIX_DETAILS.items() if k != "genres"}
    respx.get(f"{TMDB}/movie/603").mock(return_value=httpx.Response(200, json=details))
    respx.get(OMDB).mock(return_value=httpx.Response(200, json={"Response": "False"}))

    stats = await enrich_catalog(get_config())
    assert stats.merged == 1

    with session_scope() as s:
        t = s.query(Title).one()
        assert t.followed is True and t.arr_has_file is True
        assert t.collection_name == "only the duplicate" and t.certification == "R"
        assert t.imdb_rating == 8.7  # canonical's value isn't overwritten
        assert [g.name for g in t.genres] == ["Cyberpunk"]


def test_merge_moves_dup_only_ids_past_the_unique_index(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title
    from homeTheater.metadata.service import merge_titles

    init_db()
    with session_scope() as s:
        canonical = Title(title="A", kind=TitleKind.movie)
        dup = Title(title="A", kind=TitleKind.movie, tmdb_id=9, imdb_id="tt9")
        s.add_all([canonical, dup])
        s.flush()
        merge_titles(s, dup=dup, canonical=canonical)
    with session_scope() as s:
        t = s.query(Title).one()
        assert (t.tmdb_id, t.imdb_id) == (9, "tt9")


# --- transient OMDb failures (L15) ---


async def _omdb(http: httpx.AsyncClient) -> object:
    from homeTheater.metadata.omdb import OMDbClient

    return await OMDbClient("omdb-secret", http).by_imdb_id("tt1")


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429),
        httpx.Response(503),
        httpx.Response(500),
        httpx.Response(401, json={"Response": "False", "Error": "Request limit reached!"}),
    ],
)
async def test_omdb_outage_and_quota_are_transient(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    from homeTheater.errors import TransientProviderError

    _setup_env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    monkeypatch.setattr("homeTheater.metadata.http.asyncio.sleep", _no_sleep)
    respx.get(OMDB).mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientProviderError) as err:
            await _omdb(http)
    assert "omdb-secret" not in str(err.value)
    assert err.value.__cause__ is None


async def _no_sleep(_seconds: float) -> None:
    return None


@respx.mock
async def test_omdb_transport_error_is_transient(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.errors import TransientProviderError

    _setup_env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    monkeypatch.setattr("homeTheater.metadata.http.asyncio.sleep", _no_sleep)
    respx.get(OMDB).mock(side_effect=httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientProviderError):
            await _omdb(http)


@respx.mock
async def test_omdb_bad_key_is_not_transient(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    respx.get(OMDB).mock(
        return_value=httpx.Response(401, json={"Response": "False", "Error": "Invalid API key!"})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError) as err:
            await _omdb(http)
    assert "omdb-secret" not in str(err.value)
    assert "omdb-secret" not in str(err.value.request.url)


@respx.mock
async def test_omdb_outage_retries_the_rating_next_run(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: an outage used to set last_enriched_at, parking the rating for
    cache_days."""

    _setup_env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title
    from homeTheater.metadata import enrich_catalog

    init_db()
    monkeypatch.setattr("homeTheater.metadata.http.asyncio.sleep", _no_sleep)
    with session_scope() as s:
        s.add(Title(title="The Matrix", year=1999, kind=TitleKind.movie))
    respx.get(f"{TMDB}/search/movie").mock(return_value=httpx.Response(200, json=MATRIX_SEARCH))
    respx.get(f"{TMDB}/movie/603").mock(return_value=httpx.Response(200, json=MATRIX_DETAILS))
    respx.get(OMDB).mock(side_effect=httpx.ConnectTimeout("slow"))

    await enrich_catalog(get_config())

    with session_scope() as s:
        title = s.query(Title).one()
        assert title.tmdb_id == 603 and title.last_enriched_at is None


# --- all-failure runs and key handling (L20, W11) ---


@respx.mock
async def test_all_titles_failing_fails_the_run(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import JobRun, RunStatus, Title
    from homeTheater.metadata import enrich_catalog

    init_db()
    with session_scope() as s:
        s.add(Title(title="A Film", kind=TitleKind.movie))
        s.add(Title(title="Another Film", kind=TitleKind.movie))
    respx.get(f"{TMDB}/search/movie").mock(return_value=httpx.Response(401))

    with pytest.raises(RuntimeError, match="Enrichment failed"):
        await enrich_catalog(get_config())
    with session_scope() as s:
        assert s.scalars(select(JobRun)).one().status is RunStatus.failed


@respx.mock
async def test_some_titles_failing_is_a_partial_run(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import JobRun, RunStatus, Title
    from homeTheater.metadata import enrich_catalog

    init_db()
    with session_scope() as s:
        s.add(Title(title="The Matrix", year=1999, kind=TitleKind.movie))
        s.add(Title(title="Broken Film", kind=TitleKind.movie))

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["query"] == "Broken Film":
            return httpx.Response(404)
        return httpx.Response(200, json=MATRIX_SEARCH)

    respx.get(f"{TMDB}/search/movie").mock(side_effect=respond)
    respx.get(f"{TMDB}/movie/603").mock(return_value=httpx.Response(200, json=MATRIX_DETAILS))
    respx.get(OMDB).mock(return_value=httpx.Response(200, json=MATRIX_OMDB))

    stats = await enrich_catalog(get_config())
    assert len(stats.errors) == 1
    with session_scope() as s:
        assert s.scalars(select(JobRun)).one().status is RunStatus.partial


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ4In0.c2lnbmF0dXJl"


@respx.mock
async def test_v4_token_goes_in_the_header_never_the_url(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db
    from homeTheater.metadata.tmdb import TMDbClient

    init_db()
    route = respx.get(f"{TMDB}/movie/603").mock(
        return_value=httpx.Response(200, json=MATRIX_DETAILS)
    )
    async with httpx.AsyncClient() as http:
        await TMDbClient(JWT, http).details(603, TitleKind.movie)

    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {JWT}"
    assert "api_key" not in request.url.params and JWT not in str(request.url)


@respx.mock
async def test_v3_key_stays_a_param_tmdb_refuses_it_as_a_bearer(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(monkeypatch)
    from homeTheater.db import init_db
    from homeTheater.metadata.tmdb import TMDbClient

    init_db()
    route = respx.get(f"{TMDB}/movie/603").mock(
        return_value=httpx.Response(200, json=MATRIX_DETAILS)
    )
    async with httpx.AsyncClient() as http:
        await TMDbClient("0123456789abcdef", http).details(603, TitleKind.movie)

    request = route.calls[0].request
    assert request.url.params["api_key"] == "0123456789abcdef"
    assert "Authorization" not in request.headers


def _chain(exc: BaseException | None) -> list[BaseException]:
    out: list[BaseException] = []
    while exc is not None:
        out.append(exc)
        exc = exc.__cause__ or exc.__context__
    return out


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [httpx.Response(404), httpx.Response(503), httpx.ConnectError("no route")],
)
async def test_v3_key_never_reaches_an_exception(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, failure: object
) -> None:
    """W11: httpx puts the URL, key included, in the message and on ``.request``.
    Traceback chains count too, so nothing may hang off ``__context__``."""

    _setup_env(monkeypatch)
    from homeTheater.db import init_db
    from homeTheater.metadata.tmdb import TMDbClient

    init_db()
    monkeypatch.setattr("homeTheater.metadata.http.asyncio.sleep", _no_sleep)
    if isinstance(failure, Exception):
        respx.get(f"{TMDB}/movie/1").mock(side_effect=failure)
    else:
        respx.get(f"{TMDB}/movie/1").mock(return_value=failure)

    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPError) as err:
            await TMDbClient("sekret-v3-key", http).details(1, TitleKind.movie)

    for exc in _chain(err.value):
        assert "sekret-v3-key" not in str(exc)
        assert isinstance(exc, httpx.HTTPError)
        assert "sekret-v3-key" not in str(exc.request.url)
    if isinstance(err.value, httpx.HTTPStatusError):
        assert err.value.response.status_code == getattr(failure, "status_code", None)
