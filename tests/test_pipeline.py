"""Execution pipeline: download window, stage/step computation, activity API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeTheater.config.settings import DownloadWindow
from homeTheater.db.models import CandidateStatus, TitleKind
from homeTheater.pipeline import _build, _Row


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


# --- download window --------------------------------------------------------


def test_window_disabled_is_always_open() -> None:
    w = DownloadWindow(enabled=False, start_hour=2, end_hour=6)
    assert all(w.is_open(h) for h in range(24))


def test_window_daytime_range() -> None:
    w = DownloadWindow(enabled=True, start_hour=2, end_hour=6)
    assert w.is_open(2) and w.is_open(5)
    assert not w.is_open(1) and not w.is_open(6) and not w.is_open(12)


def test_window_wraps_past_midnight() -> None:
    w = DownloadWindow(enabled=True, start_hour=22, end_hour=6)
    assert w.is_open(23) and w.is_open(0) and w.is_open(5)
    assert not w.is_open(6) and not w.is_open(12) and not w.is_open(21)


# --- stage / step computation -----------------------------------------------


def _row(status: CandidateStatus, *, dl_state: str, progress: float, present: list[str]) -> _Row:
    return _Row(
        candidate_id=1,
        title="X",
        year=2020,
        kind="movie",
        status=status,
        infohash="a" * 40,
        release="X.1080p",
        dl_state=dl_state,
        dl_progress=progress,
        save_path="/d",
        error=None,
        subtitle_present=present,
    )


def _steps(state) -> dict[str, str]:
    return {s.key: s.state for s in state.steps}


def test_stage_downloading() -> None:
    st = _build(
        _row(CandidateStatus.downloading, dl_state="downloading", progress=0.42, present=[]),
        None,
        ["he", "en"],
    )
    assert st.stage == "Downloading 42%"
    assert _steps(st) == {
        "grab": "done",
        "download": "active",
        "import": "pending",
        "subs": "pending",
    }


def test_stage_downloaded_awaiting_import() -> None:
    # 100% down but not yet imported (sync hasn't run): import step goes active.
    st = _build(
        _row(CandidateStatus.queued, dl_state="downloading", progress=1.0, present=[]),
        None,
        ["he"],
    )
    assert st.stage == "Downloaded — importing…"
    steps = _steps(st)
    assert steps["download"] == "done" and steps["import"] == "active"


def test_stage_imported_but_subs_missing() -> None:
    st = _build(
        _row(CandidateStatus.imported, dl_state="imported", progress=1.0, present=["he"]),
        None,
        ["he", "en"],
    )
    assert st.stage == "Fetching subtitles (en)"
    steps = _steps(st)
    assert steps["import"] == "done" and steps["subs"] == "active" and steps["download"] == "done"


def test_stage_done_when_all_subs_present() -> None:
    st = _build(
        _row(CandidateStatus.imported, dl_state="imported", progress=1.0, present=["he", "en"]),
        None,
        ["he", "en"],
    )
    assert st.stage == "Done"
    assert _steps(st)["subs"] == "done"


def test_stage_failed() -> None:
    st = _build(
        _row(CandidateStatus.failed, dl_state="failed", progress=0.1, present=[]),
        None,
        ["he", "en"],
    )
    assert st.stage == "Failed"
    assert _steps(st)["download"] == "failed"


def test_live_status_overrides_db_progress() -> None:
    from homeTheater.acquisition.torrent.base import TorrentStatus

    live = TorrentStatus(
        infohash="a" * 40,
        progress=0.75,
        downloading=True,
        complete=False,
        save_path="/d",
        down_rate=2_000_000,
        seeders=9,
        eta_seconds=120,
    )
    st = _build(
        _row(CandidateStatus.downloading, dl_state="downloading", progress=0.1, present=[]),
        live,
        ["he"],
    )
    assert st.stage == "Downloading 75%"  # live wins over the stale DB 10%
    assert st.down_rate == 2_000_000 and st.seeders == 9 and st.eta_seconds == 120


# --- activity API -----------------------------------------------------------


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "p.yaml").write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'p.db'}'}}\n"
        "acquisition: {backend: arr}\n"  # arr -> no Transmission poll in the test
        "subtitles: {languages: [he, en]}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(tmp_path / "p.yaml"))
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")


def test_activity_api_reports_in_flight_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, Download, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=1, title="Obsession", year=2023, kind=TitleKind.movie)
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id, source=CandidateSource.discovery, status=CandidateStatus.downloading
        )
        s.add(c)
        s.flush()
        s.add(
            Download(
                candidate_id=c.id,
                external_id="h",
                state="downloading",
                progress=0.3,
                release="Obsession.1080p",
            )
        )

    with TestClient(create_app()) as client:
        r = client.get("/api/activity")
        assert r.status_code == 200
        data = r.json()
        assert data["window"]["enabled"] is False
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["title"] == "Obsession" and item["stage"] == "Downloading 30%"
        assert item["subtitle_target"] == ["he", "en"]
