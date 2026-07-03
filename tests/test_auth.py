"""Dashboard auth dependency fails closed and validates the token."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from homeTheater.api.auth import require_token


def _fresh_config() -> None:
    from homeTheater.config import loader

    loader.get_config.cache_clear()


def test_rejects_missing_token(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    _fresh_config()
    with pytest.raises(HTTPException) as exc:
        require_token(x_auth_token=None)
    assert exc.value.status_code == 401


def test_accepts_valid_token(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    _fresh_config()
    require_token(x_auth_token="secret")  # should not raise


def test_fails_closed_without_configured_token(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    _fresh_config()
    with pytest.raises(HTTPException) as exc:
        require_token(x_auth_token="anything")
    assert exc.value.status_code == 503


def test_non_ascii_token_is_401_not_500(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ASCII token must be rejected cleanly, not crash the comparison."""

    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    _fresh_config()
    with pytest.raises(HTTPException) as exc:
        require_token(x_auth_token="café")
    assert exc.value.status_code == 401


def test_webhook_accepts_dedicated_webhook_token(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.api.auth import require_webhook_token

    monkeypatch.setenv("DASHBOARD_TOKEN", "dash-secret")
    monkeypatch.setenv("WEBHOOK_TOKEN", "hook-secret")
    _fresh_config()

    require_webhook_token(token="hook-secret", x_auth_token=None)  # should not raise
    require_webhook_token(token=None, x_auth_token="hook-secret")  # header form too

    # Once a dedicated webhook token exists, the dashboard token no longer opens
    # webhooks — a webhook-token leak (it rides in URLs/logs) stays contained.
    with pytest.raises(HTTPException) as exc:
        require_webhook_token(token="dash-secret", x_auth_token=None)
    assert exc.value.status_code == 401


def test_webhook_falls_back_to_dashboard_token(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.api.auth import require_webhook_token

    monkeypatch.setenv("DASHBOARD_TOKEN", "dash-secret")
    monkeypatch.delenv("WEBHOOK_TOKEN", raising=False)
    _fresh_config()

    require_webhook_token(token="dash-secret", x_auth_token=None)  # should not raise
    with pytest.raises(HTTPException) as exc:
        require_webhook_token(token="wrong", x_auth_token=None)
    assert exc.value.status_code == 401


def test_webhook_rejects_malformed_json(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage or non-object webhook bodies are a 400, not a 500."""

    from fastapi.testclient import TestClient

    from homeTheater.api import create_app
    from homeTheater.db import session as db_session

    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _fresh_config()
    db_session._engine = None
    db_session._SessionFactory = None

    with TestClient(create_app()) as client:
        r = client.post(
            "/api/webhooks/radarr?token=tok",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

        r = client.post("/api/webhooks/sonarr?token=tok", json=["a", "list"])
        assert r.status_code == 400
