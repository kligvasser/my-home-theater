"""Candidate queue API: read (open) + review actions (auth-gated). No network."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


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


def test_grab_endpoint_adds_and_queues_in_one_call(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-click grab from search: /grab adds the title AND queues it, and is
    idempotent — a second grab reuses the same candidate rather than 409-ing."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()

    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()

    calls: dict[str, object] = {}

    async def fake_add_manual(config, tmdb_id, kind, *, reuse_existing=False):  # type: ignore[no-untyped-def]
        calls["reuse_existing"] = reuse_existing
        return 42

    async def fake_queue(config, candidate_id):  # type: ignore[no-untyped-def]
        from homeTheater.acquisition.service import QueueOutcome

        calls["queued_id"] = candidate_id
        return QueueOutcome(candidate_id, True, False, None, "grabbed")

    import homeTheater.api.candidates as cand_api

    monkeypatch.setattr(cand_api, "add_manual", fake_add_manual)
    monkeypatch.setattr(cand_api, "queue_candidate", fake_queue)

    with TestClient(create_app()) as client:
        # token required
        assert client.post("/api/candidates/grab", json={"tmdb_id": 27205}).status_code == 401

        r = client.post(
            "/api/candidates/grab",
            json={"tmdb_id": 27205, "kind": "movie"},
            headers={"X-Auth-Token": "tok"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == 42 and body["queued"] is True and body["message"] == "grabbed"
        assert calls["reuse_existing"] is True  # idempotent add
        assert calls["queued_id"] == 42  # and it queued the same candidate


def test_follow_unfollow_endpoints(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Follow sets Title.followed (auto-grab), unfollow clears it; both gated."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()

    from homeTheater.api import create_app
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title, TitleKind

    init_db()

    async def fake_follow(config, tmdb_id, kind=TitleKind.series):  # type: ignore[no-untyped-def]
        with session_scope() as s:
            t = Title(
                tmdb_id=tmdb_id, title="Silo", year=2023, kind=TitleKind.series, followed=True
            )
            s.add(t)
            s.flush()
            return t.id

    import homeTheater.api.candidates as cand_api

    monkeypatch.setattr(cand_api, "follow_series", fake_follow)

    with TestClient(create_app()) as client:
        assert client.post("/api/candidates/follow", json={"tmdb_id": 125988}).status_code == 401

        r = client.post(
            "/api/candidates/follow",
            json={"tmdb_id": 125988, "kind": "series"},
            headers={"X-Auth-Token": "tok"},
        )
        assert r.status_code == 200 and r.json()["followed"] is True
        with session_scope() as s:
            t = s.scalar(select(Title).where(Title.tmdb_id == 125988))
            assert t is not None and t.followed is True

        # unfollow clears it
        r = client.post(
            "/api/candidates/unfollow",
            json={"tmdb_id": 125988, "kind": "series"},
            headers={"X-Auth-Token": "tok"},
        )
        assert r.status_code == 200 and r.json()["followed"] is False
        with session_scope() as s:
            assert s.scalar(select(Title).where(Title.tmdb_id == 125988)).followed is False

        # unknown series → 404
        assert (
            client.post(
                "/api/candidates/unfollow",
                json={"tmdb_id": 999999, "kind": "series"},
                headers={"X-Auth-Token": "tok"},
            ).status_code
            == 404
        )


def test_unfollow_stops_in_flight_autograb(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unfollowing a series also cancels its active auto-grab (the season the
    follow already queued), not just the flag — leaving imported content alone."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()

    from homeTheater.api import create_app
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import (
        Candidate,
        CandidateSource,
        CandidateStatus,
        Download,
        Title,
        TitleKind,
    )

    init_db()
    with session_scope() as s:
        t = Title(
            tmdb_id=207484,
            title="Outlander: Blood of My Blood",
            kind=TitleKind.series,
            followed=True,
        )
        s.add(t)
        s.flush()
        # S01 imported (keep), S02 downloading (stop)
        s.add(
            Candidate(
                title_id=t.id,
                season=1,
                source=CandidateSource.discovery,
                status=CandidateStatus.imported,
            )
        )
        c2 = Candidate(
            title_id=t.id,
            season=2,
            source=CandidateSource.discovery,
            status=CandidateStatus.downloading,
        )
        s.add(c2)
        s.flush()
        s.add(
            Download(
                candidate_id=c2.id, external_id="a" * 40, state="downloading", release="S02E01"
            )
        )

    import homeTheater.api.candidates as cand_api

    async def fake_cancel(config, candidate_id):  # type: ignore[no-untyped-def]
        with session_scope() as s:
            c = s.get(Candidate, candidate_id)
            c.status = CandidateStatus.cancelled
        return "Outlander: Blood of My Blood"

    monkeypatch.setattr(cand_api, "cancel_candidate", fake_cancel)

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/candidates/unfollow",
            json={"tmdb_id": 207484, "kind": "series"},
            headers={"X-Auth-Token": "tok"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["followed"] is False and body["stopped"] == 1  # S02 cancelled

    with session_scope() as s:
        by_season = {c.season: c.status for c in s.query(Candidate).all()}
        assert by_season[1] == CandidateStatus.imported  # untouched
        assert by_season[2] == CandidateStatus.cancelled  # stopped, and no dislike
        assert s.scalar(select(Title.followed)) is False


# --- audit: W2, W16, W18, S10, A15, redaction -------------------------------


AUTH = {"X-Auth-Token": "tok"}


def _app(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()
    from homeTheater.api import create_app

    return TestClient(create_app())


def _set_status(cid: int, status: str, *, download: str | None = None) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    with session_scope() as s:
        s.get(Candidate, cid).status = CandidateStatus(status)
        if download:
            s.add(Download(candidate_id=cid, external_id="a" * 40, state=download))


@pytest.mark.parametrize("action", ["restart", "cancel"])
def test_w2_imported_candidate_is_a_conflict(
    action: str, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _app(config_file, monkeypatch) as client:
        cid = _seed_candidate()
        _set_status(cid, "imported", download="imported")

        r = client.post(f"/api/candidates/{cid}/{action}", headers=AUTH)

    assert r.status_code == 409


@pytest.mark.parametrize("action", ["restart", "cancel"])
def test_w16_mid_import_is_a_conflict(
    action: str, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _app(config_file, monkeypatch) as client:
        cid = _seed_candidate()
        _set_status(cid, "downloading", download="importing")

        r = client.post(f"/api/candidates/{cid}/{action}", headers=AUTH)

    assert r.status_code == 409 and "copying" in r.json()["detail"]


def test_cancel_unknown_candidate_is_404(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _app(config_file, monkeypatch) as client:
        assert client.post("/api/candidates/999/cancel", headers=AUTH).status_code == 404


@pytest.mark.parametrize("route", ["queue", "restart"])
def test_w5_busy_acquire_is_a_conflict(
    route: str, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.locks import job_lock

    with _app(config_file, monkeypatch) as client:
        cid = _seed_candidate()
        from homeTheater.config import get_config

        with job_lock(get_config(), "acquire"):
            r = client.post(f"/api/candidates/{cid}/{route}", headers=AUTH)

    assert r.status_code == 409 and "already running" in r.json()["detail"]


def test_not_configured_is_503_on_cancel(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.errors import NotConfiguredError

    async def not_configured(*_a: object) -> str:
        raise NotConfiguredError("nothing set up")

    import homeTheater.api.candidates as cand_api

    monkeypatch.setattr(cand_api, "cancel_candidate", not_configured)
    with _app(config_file, monkeypatch) as client:
        r = client.post("/api/candidates/1/cancel", headers=AUTH)

    assert r.status_code == 503


def test_foreign_value_error_text_is_redacted(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def leaky(*_a: object) -> object:
        raise ValueError("bad response from http://x/?api_key=SECRET")

    import homeTheater.api.candidates as cand_api

    monkeypatch.setattr(cand_api, "queue_candidate", leaky)
    with _app(config_file, monkeypatch) as client:
        r = client.post("/api/candidates/1/queue", headers=AUTH)

    assert r.status_code == 400 and "SECRET" not in r.json()["detail"]


def test_manual_add_tmdb_outage_is_a_redacted_502(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    async def outage(*_a: object, **_k: object) -> int:
        raise httpx.ConnectError("http://tmdb/?api_key=SECRET unreachable")

    import homeTheater.api.candidates as cand_api

    monkeypatch.setattr(cand_api, "add_manual", outage)
    with _app(config_file, monkeypatch) as client:
        r = client.post("/api/candidates/manual", json={"tmdb_id": 1}, headers=AUTH)

    assert r.status_code == 502 and "SECRET" not in r.text


def test_s10_routes_use_dashboard_overrides(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry_run override must reach /queue, or a grab ignores the Settings page."""
    seen: list[bool] = []

    async def fake_queue(cfg, candidate_id):  # type: ignore[no-untyped-def]
        from homeTheater.acquisition.service import QueueOutcome

        seen.append(cfg.features.dry_run)
        return QueueOutcome(candidate_id, False, cfg.features.dry_run, None, "x")

    import homeTheater.api.candidates as cand_api

    monkeypatch.setattr(cand_api, "queue_candidate", fake_queue)
    with _app(config_file, monkeypatch) as client:
        from homeTheater.config import get_config

        real = get_config()
        overridden = real.model_copy(
            update={
                "features": real.features.model_copy(update={"dry_run": not real.features.dry_run})
            }
        )
        monkeypatch.setattr(cand_api, "effective_config", lambda: overridden)
        client.post("/api/candidates/1/queue", headers=AUTH)

    assert seen == [overridden.features.dry_run]


def test_w18_discover_reports_busy_instead_of_started(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.locks import job_lock

    monkeypatch.setenv("TMDB_API_KEY", "k")
    ran: list[bool] = []

    async def fake_discovery(_cfg):  # type: ignore[no-untyped-def]
        ran.append(True)

    monkeypatch.setattr("homeTheater.discovery.run_discovery", fake_discovery)
    with _app(config_file, monkeypatch) as client:
        from homeTheater.config import get_config

        with job_lock(get_config(), "discovery"):
            r = client.post("/api/candidates/discover", json={}, headers=AUTH)

    assert r.status_code == 200 and r.json()["started"] is False
    assert ran == []


def test_a15_unfollow_reports_what_it_could_not_stop(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, Title

    with _app(config_file, monkeypatch) as client:
        cid = _seed_candidate()
        _set_status(cid, "downloading", download="importing")
        with session_scope() as s:
            tmdb_id = s.get(Title, s.get(Candidate, cid).title_id).tmdb_id

        r = client.post(
            "/api/candidates/unfollow",
            json={"tmdb_id": tmdb_id, "kind": "movie"},
            headers=AUTH,
        )

    assert r.json()["stopped"] == 0 and r.json()["skipped"] == 1


def test_a17_retry_import_route(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import MARKER_NEEDS_ATTENTION, Download

    with _app(config_file, monkeypatch) as client:
        cid = _seed_candidate()
        _set_status(cid, "downloading", download="completed")
        with session_scope() as s:
            s.query(Download).one().error = f"{MARKER_NEEDS_ATTENTION} import failed"

        assert client.post(f"/api/candidates/{cid}/retry-import").status_code == 401
        r = client.post(f"/api/candidates/{cid}/retry-import", headers=AUTH)
        missing = client.post("/api/candidates/999/retry-import", headers=AUTH)

    assert r.json() == {"retrying": 1}
    assert missing.status_code == 404
    with session_scope() as s:
        assert s.query(Download).one().error is None


@pytest.mark.parametrize(
    ("status", "download"),
    [("queued", None), ("downloading", "importing"), ("approved", "completed")],
)
def test_w16_title_in_flight_cannot_be_deleted(
    status: str, download: str | None, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, Title

    with _app(config_file, monkeypatch) as client:
        cid = _seed_candidate()
        _set_status(cid, status, download=download)
        with session_scope() as s:
            title_id = s.get(Candidate, cid).title_id

        r = client.delete(f"/api/titles/{title_id}", headers=AUTH)

    assert r.status_code == 409
    with session_scope() as s:
        assert s.get(Title, title_id) is not None


def test_w16_finished_title_can_still_be_deleted_with_its_tracking(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import Candidate, Download, DownloadTracking, GrabRetry

    with _app(config_file, monkeypatch) as client:
        cid = _seed_candidate()
        _set_status(cid, "imported", download="imported")
        with session_scope() as s:
            title_id = s.get(Candidate, cid).title_id
            download_id = s.query(Download).one().id
            s.add(DownloadTracking(download_id=download_id, last_progress_at=utcnow()))
            s.add(GrabRetry(candidate_id=cid, attempts=1, next_attempt_at=utcnow()))

        assert client.delete(f"/api/titles/{title_id}", headers=AUTH).status_code == 200

    with session_scope() as s:
        assert s.query(DownloadTracking).count() == 0
        assert s.query(GrabRetry).count() == 0
