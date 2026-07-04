"""Naming policy push to Radarr/Sonarr/Bazarr (all HTTP mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

TOKEN = {"X-Auth-Token": "test-token"}


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


@respx.mock
async def test_apply_naming_policy(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADARR_URL", "http://radarr.local")
    monkeypatch.setenv("RADARR_API_KEY", "rk")
    monkeypatch.setenv("SONARR_URL", "http://sonarr.local")
    monkeypatch.setenv("SONARR_API_KEY", "sk")
    monkeypatch.setenv("BAZARR_URL", "http://bazarr.local")
    monkeypatch.setenv("BAZARR_API_KEY", "bk")
    _reset()
    from homeTheater.acquisition.naming import apply_naming_policy
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    init_db()
    respx.get("http://radarr.local/api/v3/config/naming").mock(
        return_value=httpx.Response(200, json={"id": 1, "renameMovies": False})
    )
    radarr_put = respx.put("http://radarr.local/api/v3/config/naming").mock(
        return_value=httpx.Response(202, json={})
    )
    respx.get("http://sonarr.local/api/v3/config/naming").mock(
        return_value=httpx.Response(200, json={"id": 1, "renameEpisodes": False})
    )
    sonarr_put = respx.put("http://sonarr.local/api/v3/config/naming").mock(
        return_value=httpx.Response(202, json={})
    )
    bazarr_post = respx.post("http://bazarr.local/api/system/settings").mock(
        return_value=httpx.Response(204)
    )

    report = await apply_naming_policy(get_config())

    assert report.radarr.startswith("applied") and report.sonarr.startswith("applied")
    assert report.bazarr.startswith("applied")

    radarr_body = json.loads(radarr_put.calls.last.request.content)
    assert radarr_body["renameMovies"] is True
    assert radarr_body["movieFolderFormat"] == "{Movie Title} ({Release Year})"
    sonarr_body = json.loads(sonarr_put.calls.last.request.content)
    assert sonarr_body["seasonFolderFormat"] == "Season {season:00}"
    assert sonarr_body["renameEpisodes"] is True
    assert b"Subs" in bazarr_post.calls.last.request.content


async def test_apply_naming_requires_a_service(config_file: Path) -> None:
    _reset()
    from homeTheater.acquisition.naming import apply_naming_policy
    from homeTheater.config import get_config
    from homeTheater.errors import NotConfiguredError

    with pytest.raises(NotConfiguredError):
        await apply_naming_policy(get_config())


def test_naming_endpoint_gated(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        assert client.post("/api/settings/naming").status_code == 401
        # token but nothing configured -> 503 with guidance
        assert client.post("/api/settings/naming", headers=TOKEN).status_code == 503


def test_subtitles_override_roundtrip(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        r = client.put(
            "/api/settings",
            json={"subtitles": {"languages": ["he", "en", "fr"]}},
            headers=TOKEN,
        )
        assert r.status_code == 200
        assert r.json()["effective"]["subtitles"]["languages"] == ["he", "en", "fr"]
