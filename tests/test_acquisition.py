"""Acquisition: Radarr client, dry-run vs real queue, sync — Radarr mocked via respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from homeTheater.db.models import TitleKind

RADARR = "http://radarr.local"

LOOKUP = [{"tmdbId": 603, "title": "The Matrix", "year": 1999, "titleSlug": "the-matrix-603"}]
CREATED = {"id": 42, "title": "The Matrix"}
PROFILES = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
ROOTS = [{"path": "/movies"}]


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _write_config(tmp_path: Path, dry_run: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "acq.yaml"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'acq.db'}'}}\n"
        f"features: {{dry_run: {str(dry_run).lower()}, auto_approve: false}}\n"
        "acquisition: {movie_quality_profile: HD-1080p, search_on_add: true}\n"
    )
    # monkeypatch (not os.environ) so the override never leaks to other tests
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))


def _seed_approved(tmdb_id: int = 603) -> int:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=tmdb_id, title="The Matrix", year=1999, kind=TitleKind.movie)
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id, source=CandidateSource.discovery, status=CandidateStatus.approved
        )
        s.add(c)
        s.flush()
        return c.id


@pytest.fixture
def radarr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADARR_URL", RADARR)
    monkeypatch.setenv("RADARR_API_KEY", "rk")
    monkeypatch.setenv("TMDB_API_KEY", "tk")


@respx.mock
async def test_radarr_client_add(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    _reset()
    from homeTheater.acquisition import RadarrClient

    respx.get(f"{RADARR}/api/v3/movie/lookup").mock(return_value=httpx.Response(200, json=LOOKUP))
    respx.get(f"{RADARR}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=PROFILES)
    )
    respx.get(f"{RADARR}/api/v3/rootfolder").mock(return_value=httpx.Response(200, json=ROOTS))
    post = respx.post(f"{RADARR}/api/v3/movie").mock(return_value=httpx.Response(201, json=CREATED))

    async with httpx.AsyncClient() as http:
        client = RadarrClient(RADARR, "rk", http)
        result = await client.add(603, quality_profile="HD-1080p", root_folder=None, search=True)

    assert result.external_id == 42
    assert post.called
    body = post.calls.last.request.content
    assert b'"qualityProfileId": 4' in body or b'"qualityProfileId":4' in body
    assert b"/movies" in body


@respx.mock
async def test_queue_dry_run_changes_nothing(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, dry_run=True, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()
    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    outcome = await queue_candidate(get_config(), cid)

    assert outcome.dry_run and not outcome.queued
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.approved  # unchanged
        assert s.query(Download).count() == 0  # nothing grabbed


@respx.mock
async def test_queue_real_adds_and_records(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()

    respx.get(f"{RADARR}/api/v3/movie/lookup").mock(return_value=httpx.Response(200, json=LOOKUP))
    respx.get(f"{RADARR}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=PROFILES)
    )
    respx.get(f"{RADARR}/api/v3/rootfolder").mock(return_value=httpx.Response(200, json=ROOTS))
    respx.post(f"{RADARR}/api/v3/movie").mock(return_value=httpx.Response(201, json=CREATED))

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    outcome = await queue_candidate(get_config(), cid)

    assert outcome.queued and outcome.external_id == 42
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.queued
        dl = s.query(Download).one()
        assert dl.external_id == "42"


@respx.mock
async def test_sync_marks_completed(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()

    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    with session_scope() as s:
        s.add(
            Download(candidate_id=cid, external_id="42", state="downloading", release="The Matrix")
        )
        s.get(Candidate, cid).status = CandidateStatus.downloading

    respx.get(f"{RADARR}/api/v3/movie/42").mock(
        return_value=httpx.Response(200, json={"monitored": True, "hasFile": True})
    )
    respx.get(f"{RADARR}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))

    from homeTheater.acquisition import sync_downloads

    stats = await sync_downloads(get_config())
    assert stats.completed == 1
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.imported
        assert s.query(Download).one().state == "completed"


def test_queue_endpoint_auth(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _write_config(tmp_path, dry_run=True, monkeypatch=monkeypatch)  # dry-run: no external calls
    _reset()
    cid = _seed_approved()

    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        assert client.post(f"/api/candidates/{cid}/queue").status_code == 401
        r = client.post(f"/api/candidates/{cid}/queue", headers={"X-Auth-Token": "tok"})
        assert r.status_code == 200
        assert r.json()["dry_run"] is True and r.json()["queued"] is False


@respx.mock
async def test_queue_is_idempotent(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Double-clicking Queue must not create duplicate Download rows."""

    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()

    respx.get(f"{RADARR}/api/v3/movie/lookup").mock(return_value=httpx.Response(200, json=LOOKUP))
    respx.get(f"{RADARR}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=PROFILES)
    )
    respx.get(f"{RADARR}/api/v3/rootfolder").mock(return_value=httpx.Response(200, json=ROOTS))
    respx.post(f"{RADARR}/api/v3/movie").mock(return_value=httpx.Response(201, json=CREATED))

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    first = await queue_candidate(get_config(), cid)
    second = await queue_candidate(get_config(), cid)

    assert first.queued
    assert not second.queued and "already" in second.message
    with session_scope() as s:
        assert s.query(Download).count() == 1
        assert s.get(Candidate, cid).status == CandidateStatus.queued


async def test_queue_rejected_raises(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus
    from homeTheater.errors import InvalidTransitionError

    with session_scope() as s:
        s.get(Candidate, cid).status = CandidateStatus.rejected

    with pytest.raises(InvalidTransitionError):
        await queue_candidate(get_config(), cid)


@respx.mock
async def test_sync_does_not_resurrect_rejected(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting after a grab: sync must not flip the candidate to imported."""

    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()

    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    with session_scope() as s:
        s.add(
            Download(candidate_id=cid, external_id="42", state="downloading", release="The Matrix")
        )
        s.get(Candidate, cid).status = CandidateStatus.rejected

    respx.get(f"{RADARR}/api/v3/movie/42").mock(
        return_value=httpx.Response(200, json={"monitored": True, "hasFile": True})
    )
    respx.get(f"{RADARR}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))

    from homeTheater.acquisition import sync_downloads

    stats = await sync_downloads(get_config())
    assert stats.completed == 0
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.rejected
        assert s.query(Download).one().state == "cancelled"


@respx.mock
async def test_sync_marks_stale_download_failed(
    tmp_path: Path, radarr_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download gone from the arr queue with no file eventually fails."""

    from datetime import UTC, datetime, timedelta

    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()

    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    with session_scope() as s:
        dl = Download(candidate_id=cid, external_id="42", state="downloading", release="The Matrix")
        s.add(dl)
        s.flush()
        dl.created_at = datetime.now(UTC) - timedelta(hours=7)
        s.get(Candidate, cid).status = CandidateStatus.downloading

    respx.get(f"{RADARR}/api/v3/movie/42").mock(
        return_value=httpx.Response(200, json={"monitored": True, "hasFile": False})
    )
    respx.get(f"{RADARR}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))

    from homeTheater.acquisition import sync_downloads

    stats = await sync_downloads(get_config())
    assert stats.failed == 1
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.failed
        assert s.query(Download).one().state == "failed"


def test_sonarr_series_complete_semantics() -> None:
    from homeTheater.acquisition.arr import _series_complete

    assert not _series_complete({"episodeFileCount": 1, "episodeCount": 10})
    assert _series_complete({"episodeFileCount": 10, "episodeCount": 10})
    assert _series_complete({"percentOfEpisodes": 100.0, "episodeFileCount": 1})
    assert not _series_complete({"percentOfEpisodes": 10.0, "episodeFileCount": 1})
    assert not _series_complete({})
