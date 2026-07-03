"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


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
    # Ensure Secrets don't pick up a developer's real .env during tests.
    os.environ.setdefault("DASHBOARD_TOKEN", "test-token")
    try:
        yield cfg
    finally:
        if prev is None:
            os.environ.pop("HOME_THEATER_CONFIG", None)
        else:
            os.environ["HOME_THEATER_CONFIG"] = prev
