"""Subtitle coverage query + page/API (catalog-based, no Bazarr)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _seed() -> None:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title, TitleKind

    init_db()
    with session_scope() as s:
        have = Title(title="Has Hebrew", year=2001, kind=TitleKind.movie)
        have.owned_files = [OwnedFile(path="/a.mkv", kind=TitleKind.movie, subtitle_langs=["he"])]
        missing = Title(title="No Hebrew", year=2002, kind=TitleKind.movie)
        missing.owned_files = [
            OwnedFile(path="/b.mkv", kind=TitleKind.movie, subtitle_langs=["en"])
        ]
        s.add_all([have, missing])


def test_list_missing_subtitles(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.dashboard import list_missing_subtitles

    rows = list_missing_subtitles(lang="he")
    assert [r.title for r in rows] == ["No Hebrew"]


def test_subtitles_page_and_api(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()
    _seed()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        page = client.get("/subtitles")
        assert page.status_code == 200
        assert "Subtitle coverage" in page.text
        assert "No Hebrew" in page.text

        missing = client.get("/api/subtitles/missing").json()
        assert missing["lang"] == "he" and missing["count"] == 1

        cov = client.get("/api/subtitles/coverage").json()
        assert cov["total"] == 2 and cov["covered"] == 1 and cov["pct"] == 50.0

        # Bazarr not configured -> search endpoint fails closed (needs token first).
        assert client.post("/api/subtitles/search").status_code == 401
        r = client.post("/api/subtitles/search", headers={"X-Auth-Token": "tok"})
        assert r.status_code == 503  # BAZARR_URL not set
