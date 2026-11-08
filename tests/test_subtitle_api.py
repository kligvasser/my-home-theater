"""POST /api/subtitles/search and the effective-config read endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

HEADERS = {"X-Auth-Token": "tok"}


def _client(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from homeTheater.api import create_app
    from homeTheater.config import loader
    from homeTheater.db import init_db
    from homeTheater.db import session as db_session

    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    init_db()
    return TestClient(create_app())


def test_busy_sweep_is_409(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import job_lock
    from homeTheater.subtitles.service import SWEEP_LOCK

    with _client(config_file, monkeypatch) as client, job_lock(get_config(), SWEEP_LOCK):
        r = client.post("/api/subtitles/search", headers=HEADERS)
    assert r.status_code == 409


def test_unconfigured_is_503_with_no_run(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import JobRun

    with _client(config_file, monkeypatch) as client:
        r = client.post("/api/subtitles/search", headers=HEADERS)
    assert r.status_code == 503
    with session_scope() as s:
        assert s.query(JobRun).count() == 0


def test_sweep_runs_in_the_background(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.api import subtitles as api

    finish = asyncio.Event()
    outcome: list[str] = []

    async def slow_sweep(_config: Any, *, on_started: Any) -> None:
        on_started()
        await finish.wait()
        outcome.append("finished")

    monkeypatch.setattr(api, "sweep_subtitles", slow_sweep)
    with _client(config_file, monkeypatch) as client:
        r = client.post("/api/subtitles/search", headers=HEADERS)  # returns while it runs
        assert r.status_code == 202 and r.json()["status"] == "started"
        assert outcome == []
        assert len(api._background) == 1
        client.portal.call(finish.set)  # type: ignore[union-attr]
        client.portal.call(asyncio.sleep, 0.01)  # type: ignore[union-attr]
    assert outcome == ["finished"]
    assert not api._background


def test_failure_after_start_does_not_escape(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.api import subtitles as api

    async def failing_sweep(_config: Any, *, on_started: Any) -> None:
        on_started()
        raise RuntimeError("Native subtitle sweep failed: []")

    monkeypatch.setattr(api, "sweep_subtitles", failing_sweep)
    with _client(config_file, monkeypatch) as client:
        assert client.post("/api/subtitles/search", headers=HEADERS).status_code == 202


def test_search_uses_runtime_overrides(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.api import subtitles as api
    from homeTheater.config import save_overrides

    seen: list[list[str]] = []

    async def sweep(config: Any, *, on_started: Any) -> None:
        seen.append(config.subtitles.languages)
        on_started()

    monkeypatch.setattr(api, "sweep_subtitles", sweep)
    with _client(config_file, monkeypatch) as client:
        save_overrides({"subtitles": {"languages": ["fr"]}})
        assert client.post("/api/subtitles/search", headers=HEADERS).status_code == 202
        assert client.get("/api/subtitles/missing").json()["lang"] == "fr"
        assert client.get("/api/subtitles/coverage").json()["lang"] == "fr"
    assert seen == [["fr"]]
