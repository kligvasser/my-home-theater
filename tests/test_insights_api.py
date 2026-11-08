"""Insights/settings routes: dashboard overrides apply, and errors stay clean."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

TMDB = "https://api.themoviedb.org/3"
TOKEN = {"X-Auth-Token": "test-token"}


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _client() -> TestClient:
    from homeTheater.api.app import create_app

    return TestClient(create_app())


def test_insights_honour_dashboard_overrides(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S10: min_library set on the dashboard was ignored here."""

    _reset()
    from homeTheater.config import save_overrides
    from homeTheater.db import init_db

    init_db()
    save_overrides({"taste": {"min_library": 3}})
    seen: list[int] = []

    def fake_index(kind: object, *, min_library: int) -> None:
        seen.append(min_library)

    monkeypatch.setattr("homeTheater.taste.build_index", fake_index)
    assert _client().get("/api/insights", headers=TOKEN).status_code == 200
    assert seen == [3, 3]


def test_train_route_uses_effective_config(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    from homeTheater.config import save_overrides
    from homeTheater.db import init_db
    from homeTheater.preferences import TrainStats

    init_db()
    save_overrides({"taste": {"min_library": 3}})
    seen: list[int] = []

    def fake_train(cfg: object) -> TrainStats:
        seen.append(cfg.taste.min_library)  # type: ignore[attr-defined]
        return TrainStats()

    monkeypatch.setattr("homeTheater.preferences.train", fake_train)
    assert _client().post("/api/preferences/train", headers=TOKEN).status_code == 200
    assert seen == [3]


class _Index:
    size = 9


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [httpx.Response(500), httpx.ConnectError("no route")],
)
def test_similarity_reports_tmdb_trouble_as_502(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, failure: object
) -> None:
    """A transport error fell through to a 500 with a traceback."""

    monkeypatch.setenv("TMDB_API_KEY", "sekret-key")
    _reset()
    from homeTheater.db import init_db

    init_db()
    monkeypatch.setattr("homeTheater.taste.build_index", lambda *_a, **_k: _Index())
    monkeypatch.setattr("homeTheater.metadata.http.asyncio.sleep", _no_sleep)
    route = respx.get(f"{TMDB}/movie/5")
    if isinstance(failure, Exception):
        route.mock(side_effect=failure)
    else:
        route.mock(return_value=failure)

    response = _client().get("/api/similarity?tmdb_id=5", headers=TOKEN)

    assert response.status_code == 502
    assert "sekret-key" not in response.text


async def _no_sleep(_seconds: float) -> None:
    return None


def test_similarity_not_configured_detail_is_redacted(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "k")
    _reset()
    from homeTheater.db import init_db
    from homeTheater.errors import NotConfiguredError
    from homeTheater.metadata.tmdb import TMDbClient

    init_db()
    monkeypatch.setattr("homeTheater.taste.build_index", lambda *_a, **_k: _Index())

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise NotConfiguredError("bad url https://x/?api_key=SEKRET")

    monkeypatch.setattr(TMDbClient, "details", boom)
    response = _client().get("/api/similarity?tmdb_id=5", headers=TOKEN)
    assert response.status_code == 503 and "SEKRET" not in response.text


def test_settings_validation_detail_is_redacted(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    from homeTheater.api import settings as api_settings
    from homeTheater.config import OverrideError
    from homeTheater.db import init_db

    init_db()

    def refuse(_overrides: object) -> None:
        raise OverrideError("bad value api_key=SEKRET")

    monkeypatch.setattr(api_settings, "save_overrides", refuse)
    response = _client().put("/api/settings", json={"x": 1}, headers=TOKEN)
    assert response.status_code == 422 and "SEKRET" not in response.text


def test_naming_not_configured_detail_is_redacted(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    from homeTheater.db import init_db
    from homeTheater.errors import NotConfiguredError

    init_db()

    async def boom(_cfg: object) -> None:
        raise NotConfiguredError("https://x/?apikey=SEKRET")

    monkeypatch.setattr("homeTheater.acquisition.naming.apply_naming_policy", boom)
    response = _client().post("/api/settings/naming", headers=TOKEN)
    assert response.status_code == 503 and "SEKRET" not in response.text
