"""Read-only dashboard: queries, HTML pages, and JSON API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homeTheater.dashboard import human_size


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _seed() -> None:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Genre, OwnedFile, Title, TitleKind

    init_db()
    with session_scope() as s:
        matrix = Title(
            title="The Matrix",
            year=1999,
            kind=TitleKind.movie,
            imdb_rating=8.7,
            imdb_votes=1_900_000,
        )
        matrix.genres = [Genre(name="Action"), Genre(name="Sci-Fi")]
        matrix.owned_files = [
            OwnedFile(
                path="/Movies/matrix.mkv",
                kind=TitleKind.movie,
                resolution="1080p",
                size_bytes=8_000_000_000,
                subtitle_langs=["he", "en"],
            )
        ]
        bb = Title(title="Breaking Bad", year=2008, kind=TitleKind.series)
        bb.owned_files = [
            OwnedFile(
                path="/TV/bb.mkv",
                kind=TitleKind.series,
                resolution="720p",
                size_bytes=2_000_000_000,
                subtitle_langs=[],
            )
        ]
        s.add_all([matrix, bb])


def test_human_size() -> None:
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(8_000_000_000).endswith("GB")


def test_get_stats(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.dashboard import get_stats

    stats = get_stats()
    assert stats.total_titles == 2
    assert stats.movies == 1 and stats.series == 1
    assert stats.files == 2
    assert stats.total_size_bytes == 10_000_000_000
    assert ("1080p", 1) in stats.resolutions and ("720p", 1) in stats.resolutions
    assert ("Action", 1) in stats.genres
    assert dict(stats.decades) == {1990: 1, 2000: 1}
    # Only The Matrix has a Hebrew sidecar -> 1 of 2 owned titles.
    assert stats.coverage.covered == 1 and stats.coverage.total == 2
    assert stats.coverage.pct == 50.0


def test_list_titles_search_and_filter(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.dashboard import list_titles

    rows, total = list_titles(q="matrix")
    assert total == 1 and rows[0].title == "The Matrix"
    assert rows[0].has_sub is True
    assert set(rows[0].genres) == {"Action", "Sci-Fi"}

    rows, total = list_titles(kind="series")
    assert total == 1 and rows[0].title == "Breaking Bad"
    assert rows[0].has_sub is False


def test_html_pages_render(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "Library at a glance" in r.text
        assert "Recently added" in r.text  # poster wall shows the owned title
        assert "The Matrix" in r.text

        r = client.get("/library?q=matrix")
        assert r.status_code == 200
        assert "The Matrix" in r.text

        r = client.get("/runs")
        assert r.status_code == 200


def test_unknown_status_filter_is_handled(config_file: Path) -> None:
    """Bad ?status= values must not 500: HTML falls back, JSON API rejects."""

    _reset()
    _seed()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        r = client.get("/candidates", params={"status": "bogus"})
        assert r.status_code == 200  # falls back to the "new" queue

        r = client.get("/api/candidates", params={"status": "bogus"})
        assert r.status_code == 422  # validated against CandidateStatus


def test_json_api(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        stats = client.get("/api/stats").json()
        assert stats["total_titles"] == 2
        assert stats["coverage"]["pct"] == 50.0

        titles = client.get("/api/titles", params={"kind": "movie"}).json()
        assert titles["total"] == 1
        assert titles["items"][0]["title"] == "The Matrix"
