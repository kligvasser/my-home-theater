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
