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
