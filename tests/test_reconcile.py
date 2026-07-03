"""Import reconciliation: payload parsing, reconcile_import idempotency, webhook."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeTheater.db.models import TitleKind

RADARR_IMPORT = {
    "eventType": "Download",
    "movie": {"id": 1, "title": "The Matrix", "year": 1999, "tmdbId": 603, "imdbId": "tt0133093"},
    "movieFile": {
        "path": "/movies/The Matrix (1999)/The Matrix (1999) Bluray-1080p.mkv",
        "size": 8_000_000_000,
        "quality": {"quality": {"name": "Bluray-1080p", "resolution": 1080}},
    },
}
SONARR_IMPORT = {
    "eventType": "Download",
    "series": {"id": 2, "title": "Fauda", "tvdbId": 300000, "tmdbId": 60000},
    "episodes": [{"seasonNumber": 1, "episodeNumber": 3}],
    "episodeFile": {
        "path": "/tv/Fauda/Season 01/Fauda S01E03 WEBDL-720p.mkv",
        "size": 1_500_000_000,
        "quality": {"quality": {"name": "WEBDL-720p"}},
    },
}
RADARR_TEST = {"eventType": "Test"}


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def test_parse_radarr_import() -> None:
    from homeTheater.reconcile import parse_radarr

    ev = parse_radarr(RADARR_IMPORT)
    assert ev is not None
    assert ev.kind is TitleKind.movie
    assert ev.tmdb_id == 603 and ev.imdb_id == "tt0133093"
    assert ev.resolution == "1080p"
    assert ev.size_bytes == 8_000_000_000


def test_parse_sonarr_import() -> None:
    from homeTheater.reconcile import parse_sonarr

    ev = parse_sonarr(SONARR_IMPORT)
    assert ev is not None
    assert ev.kind is TitleKind.series
    assert ev.tvdb_id == 300000
    assert ev.season == 1 and ev.episode == 3
    assert ev.resolution == "720p"


def test_non_import_events_ignored() -> None:
    from homeTheater.reconcile import parse_radarr

    assert parse_radarr(RADARR_TEST) is None


def test_reconcile_import_links_and_flips_candidate(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title
    from homeTheater.reconcile import parse_radarr, reconcile_import

    init_db()
    # Seed a queued candidate for the movie we're about to import.
    with session_scope() as s:
        t = Title(tmdb_id=603, title="The Matrix", year=1999, kind=TitleKind.movie)
        s.add(t)
        s.flush()
        s.add(
            Candidate(
                title_id=t.id, source=CandidateSource.discovery, status=CandidateStatus.queued
            )
        )

    ev = parse_radarr(RADARR_IMPORT)
    assert ev is not None
    result = reconcile_import(ev)

    assert result.file_created and result.candidate_imported
    with session_scope() as s:
        from homeTheater.db.models import OwnedFile

        of = s.query(OwnedFile).one()
        assert of.resolution == "1080p" and of.size_bytes == 8_000_000_000
        cand = s.query(Candidate).one()
        assert cand.status == CandidateStatus.imported


def test_reconcile_import_is_idempotent(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.reconcile import parse_radarr, reconcile_import

    init_db()
    ev = parse_radarr(RADARR_IMPORT)
    assert ev is not None
    first = reconcile_import(ev)
    second = reconcile_import(ev)

    assert first.file_created and not second.file_created  # same path, no dup
    with session_scope() as s:
        assert s.query(OwnedFile).count() == 1
        assert s.query(Title).count() == 1


def test_webhook_endpoint(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        # No token -> rejected.
        assert client.post("/api/webhooks/radarr", json=RADARR_IMPORT).status_code == 401

        # Token via query param (as arr would send it).
        r = client.post("/api/webhooks/radarr?token=tok", json=RADARR_IMPORT)
        assert r.status_code == 200 and r.json()["handled"] is True
        assert r.json()["file_created"] is True

        # Test event acknowledged but not handled.
        r = client.post("/api/webhooks/radarr?token=tok", json=RADARR_TEST)
        assert r.status_code == 200 and r.json()["handled"] is False


SONARR_MULTI = {
    "eventType": "Download",
    "series": {"id": 2, "title": "Fauda", "tvdbId": 300000, "tmdbId": 60000},
    "episodes": [
        {"seasonNumber": 1, "episodeNumber": 2},
        {"seasonNumber": 1, "episodeNumber": 1},
    ],
    "episodeFile": {"path": "/tv/Fauda/Season 01/Fauda S01E01E02.mkv", "size": 3_000_000_000},
}


def test_parse_sonarr_multi_episode_file() -> None:
    from homeTheater.reconcile import parse_sonarr

    ev = parse_sonarr(SONARR_MULTI)
    assert ev is not None
    assert ev.season == 1 and ev.episode == 1 and ev.episode_end == 2


def test_parse_radarr_without_file_path_has_no_path() -> None:
    """folderPath must not be recorded as an owned *file*."""

    from homeTheater.reconcile import parse_radarr

    payload = {
        "eventType": "Download",
        "movie": {"id": 1, "title": "X", "tmdbId": 9, "folderPath": "/movies/X (2020)"},
    }
    ev = parse_radarr(payload)
    assert ev is not None and ev.path is None


def test_reconcile_does_not_flip_kind_of_same_tmdb_id(config_file: Path) -> None:
    """A Sonarr import whose series tmdbId equals an existing movie's id must
    create a new series row, not rewrite the movie."""

    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title
    from homeTheater.reconcile import parse_sonarr, reconcile_import

    init_db()
    with session_scope() as s:
        s.add(Title(tmdb_id=60000, title="Some Movie", year=2001, kind=TitleKind.movie))

    ev = parse_sonarr(SONARR_IMPORT)
    assert ev is not None
    reconcile_import(ev)

    with session_scope() as s:
        titles = s.query(Title).all()
        assert len(titles) == 2
        kinds = {t.kind for t in titles}
        assert kinds == {TitleKind.movie, TitleKind.series}


def test_reconcile_id_backfill_skips_conflicts(config_file: Path) -> None:
    """A webhook whose ids straddle two existing rows must not 500 on the
    unique index — the conflicting backfill is skipped."""

    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title
    from homeTheater.reconcile import parse_radarr, reconcile_import

    init_db()
    with session_scope() as s:
        # Row A holds the imdb_id but no tmdb_id; row B holds the tmdb_id.
        s.add(Title(imdb_id="tt0133093", title="A", kind=TitleKind.movie))
        s.add(Title(tmdb_id=603, title="B", kind=TitleKind.movie))

    ev = parse_radarr(RADARR_IMPORT)
    assert ev is not None
    result = reconcile_import(ev)  # must not raise IntegrityError

    with session_scope() as s:
        matched = s.get(Title, result.title_id)
        # tmdb lookup wins -> row B; imdb backfill would conflict with A -> skipped
        assert matched.tmdb_id == 603
        assert matched.imdb_id is None


def test_reconcile_import_sets_arr_flag(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title
    from homeTheater.reconcile import parse_radarr, reconcile_import

    init_db()
    ev = parse_radarr(RADARR_IMPORT)
    assert ev is not None
    reconcile_import(ev)
    with session_scope() as s:
        assert s.query(Title).one().arr_has_file is True


async def test_reconcile_library_marks_arr_owned_and_prunes_flag(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library poll flags arr-owned titles (so discovery skips them), records
    Radarr file paths as owned files, and clears the flag when items disappear."""

    import httpx
    import respx

    monkeypatch.setenv("RADARR_URL", "http://radarr.local")
    monkeypatch.setenv("RADARR_API_KEY", "rk")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.reconcile import reconcile_library

    init_db()
    movie = {
        "id": 7,
        "title": "The Matrix",
        "tmdbId": 603,
        "hasFile": True,
        "movieFile": {"path": "/movies/The Matrix (1999)/matrix.mkv"},
    }
    with respx.mock:
        respx.get("http://radarr.local/api/v3/movie").mock(
            return_value=httpx.Response(200, json=[movie])
        )
        stats = await reconcile_library(get_config())

    assert stats.titles_created == 1 and stats.arr_flag_set == 1 and stats.files_created == 1
    with session_scope() as s:
        t = s.query(Title).one()
        assert t.arr_has_file is True
        assert s.query(OwnedFile).one().path == "/movies/The Matrix (1999)/matrix.mkv"

    # Movie deleted from Radarr -> flag cleared on the next poll.
    with respx.mock:
        respx.get("http://radarr.local/api/v3/movie").mock(
            return_value=httpx.Response(200, json=[])
        )
        stats2 = await reconcile_library(get_config())
    assert stats2.arr_flag_cleared == 1
    with session_scope() as s:
        assert s.query(Title).one().arr_has_file is False
