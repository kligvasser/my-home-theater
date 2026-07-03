"""Phase 9 hardening: provider health, status surfacing, DB backup."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


@respx.mock
async def test_health_checks(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "k")  # only TMDb configured
    _reset()
    from homeTheater.config import get_config
    from homeTheater.health import check_all

    respx.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=httpx.Response(200, json={})
    )

    by_name = {s.name: s for s in await check_all(get_config())}
    assert by_name["tmdb"].configured and by_name["tmdb"].ok is True
    assert by_name["radarr"].configured is False  # not set -> not probed
    assert by_name["smb"].ok is None  # configured-only, never probed


@respx.mock
async def test_health_reports_unreachable(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "k")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.health import check_all

    respx.get("https://api.themoviedb.org/3/configuration").mock(return_value=httpx.Response(401))
    by_name = {s.name: s for s in await check_all(get_config())}
    assert by_name["tmdb"].configured and by_name["tmdb"].ok is False


@respx.mock
async def test_health_detail_redacts_api_key(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx errors embed the request URL (key included); detail must not."""

    monkeypatch.setenv("TMDB_API_KEY", "sekret123")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.health import check_all

    respx.get("https://api.themoviedb.org/3/configuration").mock(return_value=httpx.Response(401))
    by_name = {s.name: s for s in await check_all(get_config())}
    tmdb = by_name["tmdb"]
    assert tmdb.ok is False
    assert "sekret123" not in tmdb.detail
    assert "api_key=REDACTED" in tmdb.detail


@respx.mock
async def test_health_results_are_cached(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unauthenticated status hits must not probe providers on every call."""

    monkeypatch.setenv("TMDB_API_KEY", "k")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.health import check_all

    route = respx.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=httpx.Response(200, json={})
    )
    first = await check_all(get_config())
    second = await check_all(get_config())
    assert route.call_count == 1
    assert first == second


def test_backup_creates_valid_snapshot(config_file: Path, tmp_path: Path) -> None:
    _reset()
    from homeTheater.backup import backup_database
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title, TitleKind

    init_db()
    with session_scope() as s:
        s.add(Title(title="The Matrix", year=1999, kind=TitleKind.movie))

    dest = backup_database(get_config(), dest_dir=tmp_path / "bk", keep=3)
    assert dest.exists()

    # The snapshot is a valid SQLite DB containing our data.
    conn = sqlite3.connect(str(dest))
    try:
        count = conn.execute("SELECT COUNT(*) FROM title").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_backup_rejects_non_sqlite(config_file: Path) -> None:
    _reset()
    from homeTheater.backup import backup_database
    from homeTheater.config.settings import AppConfig, Database, NASPaths

    cfg = AppConfig(
        nas=NASPaths(share="T", movies_root="M", tv_root="TV"),
        database=Database(url="postgresql://x/y"),
    )
    with pytest.raises(ValueError, match="SQLite"):
        backup_database(cfg)


def test_status_endpoint_and_page(config_file: Path) -> None:
    # No provider creds set -> check_all makes no network calls.
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        providers = client.get("/api/providers").json()["providers"]
        names = {p["name"] for p in providers}
        assert {"tmdb", "omdb", "radarr", "sonarr", "bazarr", "smb"} <= names

        status = client.get("/api/status").json()
        assert status["dry_run"] is True and status["scheduler"] is False

        page = client.get("/status")
        assert page.status_code == 200 and "Status" in page.text
