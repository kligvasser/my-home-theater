"""Content-based taste model: tokenization, similarity, clustering, insights."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homeTheater.db.models import TitleKind


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _seed_library() -> None:
    """Two clearly separated movie groups: dark sci-fi vs romantic comedy."""

    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title

    init_db()
    scifi = [
        ("Blade Runner", 1982, ["Science Fiction", "Thriller"], ["dystopia", "android"]),
        ("Arrival", 2016, ["Science Fiction", "Drama"], ["aliens", "linguistics"]),
        ("Ex Machina", 2014, ["Science Fiction", "Thriller"], ["android", "ai"]),
        ("Inception", 2010, ["Science Fiction", "Action"], ["dreams", "heist"]),
        ("Interstellar", 2014, ["Science Fiction", "Drama"], ["space", "time"]),
    ]
    romcom = [
        ("Notting Hill", 1999, ["Romance", "Comedy"], ["london", "bookshop"]),
        ("About Time", 2013, ["Romance", "Comedy"], ["time", "family"]),
        ("Love Actually", 2003, ["Romance", "Comedy"], ["christmas", "london"]),
        ("The Holiday", 2006, ["Romance", "Comedy"], ["christmas", "house swap"]),
    ]
    with session_scope() as s:
        from homeTheater.db.models import Genre

        genre_cache: dict[str, Genre] = {}

        def genres_for(names: list[str]) -> list[Genre]:
            out = []
            for n in names:
                if n not in genre_cache:
                    genre_cache[n] = Genre(name=n)
                    s.add(genre_cache[n])
                out.append(genre_cache[n])
            return out

        for i, (name, year, genres, keywords) in enumerate(scifi + romcom):
            t = Title(
                tmdb_id=1000 + i,
                title=name,
                year=year,
                kind=TitleKind.movie,
                keywords=keywords,
                original_language="en",
            )
            t.genres = genres_for(genres)
            t.owned_files = [OwnedFile(path=f"/m/{i}.mkv", kind=TitleKind.movie)]
            s.add(t)


def test_tokens_from_features() -> None:
    from homeTheater.taste import tokens_from_features

    tokens = tokens_from_features(
        {
            "genres": ["Science Fiction"],
            "keywords": ["android"],
            "directors": ["Denis Villeneuve"],
            "original_language": "en",
            "decade": 2010,
            "in_collection": True,
            "collection_name": "Dune",
        }
    )
    assert "g:science fiction" in tokens
    assert "kw:android" in tokens
    assert "dir:denis villeneuve" in tokens
    assert "lang:en" in tokens and "dec:2010" in tokens and "col:dune" in tokens
    assert tokens_from_features({}) == []


def test_similarity_prefers_matching_content(config_file: Path) -> None:
    _reset()
    _seed_library()
    from homeTheater.taste import build_index

    index = build_index(TitleKind.movie, min_library=5)
    assert index is not None and index.size == 9

    scifi_feats = {
        "genres": ["Science Fiction", "Thriller"],
        "keywords": ["android", "dystopia"],
        "original_language": "en",
        "decade": 2010,
    }
    romcom_feats = {
        "genres": ["Romance", "Comedy"],
        "keywords": ["christmas"],
        "original_language": "en",
        "decade": 2000,
    }
    scifi_sim = index.similarity(scifi_feats, k=3)
    romcom_sim = index.similarity(romcom_feats, k=3)
    unrelated_sim = index.similarity(
        {"genres": ["Documentary"], "keywords": ["volcanoes"], "original_language": "is"},
        k=3,
    )

    # Each query's nearest neighbors come from its own group...
    scifi_titles = {"Blade Runner", "Arrival", "Ex Machina", "Inception", "Interstellar"}
    romcoms = {"Notting Hill", "About Time", "Love Actually", "The Holiday"}
    assert set(scifi_sim.like) <= scifi_titles
    assert set(romcom_sim.like) <= romcoms
    # ...and both score far above content the library has nothing like.
    assert scifi_sim.score > unrelated_sim.score
    assert romcom_sim.score > unrelated_sim.score


def test_clusters_separate_the_groups(config_file: Path) -> None:
    _reset()
    _seed_library()
    from homeTheater.taste import build_index

    index = build_index(TitleKind.movie, min_library=5)
    assert index is not None
    clusters = index.clusters(max_clusters=4)
    assert len(clusters) >= 2
    assert sum(c.size for c in clusters) == 9
    # Some cluster is recognisably the romcom group.
    romcoms = {"Notting Hill", "About Time", "Love Actually", "The Holiday"}
    assert any(romcoms & set(c.titles) == set(c.titles) & romcoms and c.size == 4 for c in clusters)


def test_index_requires_min_library(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db

    init_db()
    from homeTheater.taste import build_index

    assert build_index(TitleKind.series, min_library=8) is None


def test_insights_api_and_page(config_file: Path) -> None:
    _reset()
    _seed_library()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/insights")
        assert r.status_code == 200
        body = r.json()
        assert body["movie"]["available"] is True
        assert body["movie"]["titles"] == 9
        assert body["series"]["available"] is False

        page = client.get("/insights")
        assert page.status_code == 200
        assert "Blade Runner" in page.text


def test_similarity_api_requires_tmdb_key(config_file: Path) -> None:
    _reset()
    _seed_library()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/similarity", params={"tmdb_id": 78, "kind": "movie"})
        assert r.status_code == 503  # no TMDB_API_KEY in test env
