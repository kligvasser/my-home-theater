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


def test_candidate_sorting_and_pagination(config_file: Path) -> None:
    _reset()
    from homeTheater.dashboard import list_candidates
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title, TitleKind

    init_db()
    # (title, year, rating, votes, release_date, score, taste)
    specs = [
        ("Alpha", 2020, 7.0, 100, "2020-01-01", 10.0, 0.1),
        ("Bravo", 2024, 9.0, 50, "2024-06-01", 5.0, 0.9),
        ("Charlie", 2022, 8.0, 999, "2022-03-15", 20.0, 0.5),
    ]
    with session_scope() as s:
        for i, (title, year, rating, votes, rel, score, taste) in enumerate(specs, start=1):
            t = Title(
                tmdb_id=i,
                title=title,
                year=year,
                kind=TitleKind.movie,
                imdb_rating=rating,
                imdb_votes=votes,
                release_date=rel,
            )
            s.add(t)
            s.flush()
            s.add(
                Candidate(
                    title_id=t.id,
                    source=CandidateSource.discovery,
                    status=CandidateStatus.new,
                    score=score,
                    features={"taste": {"score": taste}},
                )
            )

    def titles(sort: str) -> list[str]:
        rows, _ = list_candidates(status="new", sort=sort)
        return [r.title for r in rows]

    assert titles("score") == ["Charlie", "Alpha", "Bravo"]  # 20, 10, 5
    assert titles("rating") == ["Bravo", "Charlie", "Alpha"]  # 9, 8, 7
    assert titles("votes") == ["Charlie", "Alpha", "Bravo"]  # 999, 100, 50
    assert titles("release") == ["Bravo", "Charlie", "Alpha"]  # 2024, 2022, 2020
    assert titles("taste") == ["Bravo", "Charlie", "Alpha"]  # 0.9, 0.5, 0.1

    rows, total = list_candidates(status="new", sort="score", page=1, page_size=2)
    assert total == 3 and [r.title for r in rows] == ["Charlie", "Alpha"]
    rows2, _ = list_candidates(status="new", sort="score", page=2, page_size=2)
    assert [r.title for r in rows2] == ["Bravo"]
