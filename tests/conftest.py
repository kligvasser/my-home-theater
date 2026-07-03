"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Every secret the app reads from the environment / .env. Cleared per test so a
# developer's real credentials can never leak into (or be used by) a test run.
SECRET_ENV_VARS = (
    "TMDB_API_KEY",
    "OMDB_API_KEY",
    "SMB_USER",
    "SMB_PASS",
    "SMB_HOST",
    "RADARR_URL",
    "RADARR_API_KEY",
    "SONARR_URL",
    "SONARR_API_KEY",
    "BAZARR_URL",
    "BAZARR_API_KEY",
    "TRAKT_CLIENT_ID",
    "TRAKT_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DASHBOARD_TOKEN",
    "WEBHOOK_TOKEN",
)


@pytest.fixture(autouse=True)
def _isolate_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate tests from the developer's real .env and secret env vars.

    ``Secrets`` reads ``.env`` relative to the CWD, so chdir into the (empty)
    test tmp dir and scrub every secret env var. Individual tests opt back in
    with ``monkeypatch.setenv``.
    """

    monkeypatch.chdir(tmp_path)
    for var in SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # Provider health results are cached module-wide; never share across tests.
    from homeTheater.health import clear_cache

    clear_cache()


@pytest.fixture
def config_file(tmp_path: Path) -> Iterator[Path]:
    """Write a minimal valid config.yaml and point the app at it."""

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "nas:\n"
        "  share: TestShare\n"
        "  movies_root: Movies\n"
        "  tv_root: TV Shows\n"
        f"database:\n  url: sqlite:///{tmp_path / 'test.db'}\n"
    )
    prev = os.environ.get("HOME_THEATER_CONFIG")
    os.environ["HOME_THEATER_CONFIG"] = str(cfg)
    # Deterministic token for auth tests (the autouse fixture scrubbed any real one).
    os.environ["DASHBOARD_TOKEN"] = "test-token"
    try:
        yield cfg
    finally:
        if prev is None:
            os.environ.pop("HOME_THEATER_CONFIG", None)
        else:
            os.environ["HOME_THEATER_CONFIG"] = prev
        os.environ.pop("DASHBOARD_TOKEN", None)
