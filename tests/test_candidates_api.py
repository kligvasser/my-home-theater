"""Candidate queue API: read (open) + review actions (auth-gated). No network."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _seed_candidate() -> int:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title, TitleKind

    init_db()
    with session_scope() as s:
        t = Title(
            tmdb_id=155,
            title="The Dark Knight",
            year=2008,
            kind=TitleKind.movie,
            imdb_rating=9.0,
            imdb_votes=2_600_000,
        )
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id,
            source=CandidateSource.discovery,
            status=CandidateStatus.new,
            reason="IMDb 9.0 with 2,600,000 votes; via trending movie (week)",
            score=54.0,
        )
        s.add(c)
        s.flush()
        return c.id


def test_list_and_review(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()
    cid = _seed_candidate()

    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        # Read is open.
        data = client.get("/api/candidates", params={"status": "new"}).json()
        assert data["counts"]["new"] == 1
        assert data["items"][0]["title"] == "The Dark Knight"

        # Mutating without a token is rejected.
        assert client.post(f"/api/candidates/{cid}/approve").status_code == 401

        # With the token it succeeds.
        r = client.post(f"/api/candidates/{cid}/approve", headers={"X-Auth-Token": "tok"})
        assert r.status_code == 200 and r.json()["status"] == "approved"

        # Now it's gone from the 'new' queue.
        data = client.get("/api/candidates", params={"status": "new"}).json()
        assert data["counts"]["new"] == 0 and data["counts"]["approved"] == 1

        # Unknown id -> 404.
        assert (
            client.post("/api/candidates/9999/reject", headers={"X-Auth-Token": "tok"}).status_code
            == 404
        )


def test_candidates_page_renders(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()
    _seed_candidate()

    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        r = client.get("/candidates")
        assert r.status_code == 200
        assert "Candidate queue" in r.text
        assert "The Dark Knight" in r.text
