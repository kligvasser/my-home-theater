"""Execution pipeline: download window, stage/step computation, activity API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeTheater.config.settings import DownloadWindow
from homeTheater.db.models import CandidateStatus, TitleKind
from homeTheater.pipeline import _build, _Dl, _primary, _Row


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


def test_stage_importing_shows_copy_progress() -> None:
    # dl_state 'importing' + progress = NAS-copy fraction (not download %).
    st = _build(
        _row(CandidateStatus.downloading, dl_state="importing", progress=0.45, present=[]),
        None,
        ["he"],
    )
    assert st.stage == "Importing to NAS 45%"
    steps = _steps(st)
    assert steps["download"] == "done" and steps["import"] == "active"


def test_importing_ignores_live_download_progress() -> None:
    from homeTheater.acquisition.torrent.base import TorrentStatus

    live = TorrentStatus(
        infohash="a" * 40, progress=1.0, downloading=False, complete=True, save_path="/d"
    )
    st = _build(
        _row(CandidateStatus.downloading, dl_state="importing", progress=0.3, present=[]),
        live,
        ["he"],
    )
    assert st.stage == "Importing to NAS 30%"  # copy fraction, not the 100% download
    assert st.progress == 0.3


# --- season candidate: per-episode breakdown -------------------------------


def _dl(dl_id: int, release: str, state: str, progress: float) -> _Dl:
    return _Dl(
        id=dl_id,
        infohash=f"h{dl_id}",
        release=release,
        state=state,
        progress=progress,
        save_path="/d",
        error=None,
    )


def _season_row(downloads: list[_Dl], status: CandidateStatus) -> _Row:
    primary = _primary(downloads)
    assert primary is not None
    return _Row(
        candidate_id=9,
        title="Dexter: Resurrection",
        year=2025,
        kind="series",
        status=status,
        infohash=primary.infohash,
        release=primary.release,
        dl_state=primary.state,
        dl_progress=primary.progress,
        save_path=primary.save_path,
        error=primary.error,
        subtitle_present=[],
        downloads=tuple(downloads),
    )


def test_primary_tracks_least_advanced_episode() -> None:
    # E02 imported, E01 still downloading: the headline must follow E01, not the
    # newer (higher-id) imported E02 — the bug that showed the season "Done".
    e01 = _dl(1, "Dexter Resurrection S02E01 1080p WEB-CAKES", "downloading", 0.45)
    e02 = _dl(2, "Dexter Resurrection S02E02 1080p x265-MeGusta", "imported", 1.0)
    assert _primary([e01, e02]).id == e01.id


def test_season_headline_not_done_while_episode_downloads() -> None:
    e01 = _dl(1, "Dexter Resurrection S02E01 1080p WEB-CAKES", "downloading", 0.45)
    e02 = _dl(2, "Dexter Resurrection S02E02 1080p x265-MeGusta", "imported", 1.0)
    st = _build(_season_row([e01, e02], CandidateStatus.downloading), None, ["he", "en"])
    assert st.stage == "Downloading 45%"
    # Both episodes are listed, sorted, each with its own state.
    labels = [(e.label, e.stage, e.state) for e in st.episodes]
    assert labels == [
        ("S02E01", "Downloading 45%", "active"),
        ("S02E02", "Done", "done"),
    ]


def test_single_download_candidate_has_no_episode_breakdown() -> None:
    # A movie (one download) shows no per-episode list.
    st = _build(
        _row(CandidateStatus.downloading, dl_state="downloading", progress=0.3, present=[]),
        None,
        ["he"],
    )
    assert st.episodes == []


def test_imported_candidate_excluded_from_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, Download, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=2, title="Done Movie", year=2024, kind=TitleKind.movie)
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id, source=CandidateSource.discovery, status=CandidateStatus.imported
        )
        s.add(c)
        s.flush()
        s.add(Download(candidate_id=c.id, external_id="h", state="imported", progress=1.0))

    with TestClient(create_app()) as client:
        data = client.get("/api/activity").json()
        assert data["items"] == []  # imported items drop off Activity (video on NAS)
