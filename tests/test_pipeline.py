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


# --- stages follow the download state, not the rounded percentage ----------


def test_almost_finished_download_is_not_reported_finished() -> None:
    # 99.6% rounds to 100, but the bytes aren't down: no import step, no "Downloaded".
    st = _build(
        _row(CandidateStatus.downloading, dl_state="downloading", progress=0.996, present=[]),
        None,
        ["he"],
    )
    assert st.stage == "Downloading 99%"
    steps = _steps(st)
    assert steps["download"] == "active" and steps["import"] == "pending"


def test_almost_finished_episode_is_not_reported_finished() -> None:
    e01 = _dl(1, "Show S01E01 1080p", "downloading", 0.996)
    e02 = _dl(2, "Show S01E02 1080p", "imported", 1.0)
    st = _build(_season_row([e01, e02], CandidateStatus.downloading), None, [])
    assert [e.stage for e in st.episodes] == ["Downloading 99%", "Done"]


def test_completed_download_reads_as_downloaded_whatever_the_candidate_says() -> None:
    # The candidate can still be "queued"; the bytes are down and import is pending.
    st = _build(
        _row(CandidateStatus.queued, dl_state="completed", progress=0.0, present=[]), None, []
    )
    assert st.stage == "Downloaded — importing…"
    steps = _steps(st)
    assert steps["download"] == "done" and steps["import"] == "active"


def test_queued_download_of_a_downloading_candidate_reads_queued() -> None:
    st = _build(
        _row(CandidateStatus.downloading, dl_state="queued", progress=0.0, present=[]), None, []
    )
    assert st.stage == "Queued"


def test_failed_season_with_an_episode_still_importing_reports_the_import() -> None:
    st = _build(
        _row(CandidateStatus.failed, dl_state="importing", progress=0.5, present=[]), None, []
    )
    assert st.stage == "Importing to NAS 50%"
    assert _steps(st)["import"] == "active"


def test_failed_candidate_with_no_download_failed_at_the_grab() -> None:
    row = _row(CandidateStatus.failed, dl_state="x", progress=0.0, present=[])
    no_grab = _Row(**{**{f: getattr(row, f) for f in row.__slots__}, "dl_state": None})
    st = _build(no_grab, None, ["he"])
    assert st.stage == "Failed"
    assert _steps(st) == {
        "grab": "failed",
        "download": "pending",
        "import": "pending",
        "subs": "pending",
    }


def test_no_subtitle_target_never_leaves_the_subs_step_waiting() -> None:
    st = _build(
        _row(CandidateStatus.imported, dl_state="imported", progress=1.0, present=[]), None, []
    )
    assert st.stage == "Done" and _steps(st)["subs"] == "done"


# --- which rows speak for a candidate --------------------------------------


def _with_error(dl: _Dl, error: str) -> _Dl:
    return _Dl(dl.id, dl.infohash, dl.release, dl.state, dl.progress, dl.save_path, error)


def test_quarantine_record_is_not_a_failure_of_the_grab() -> None:
    # A fake was thrown out and a replacement is downloading: the ban row is history.
    ban = _with_error(_dl(1, "Show S01E01 FAKE", "failed", 0.0), "removed: torrent only an .exe")
    good = _dl(2, "Show S01E01 1080p", "downloading", 0.4)
    assert _primary([ban, good]).id == good.id
    assert _primary([ban]) is None
    st = _build(_season_row([ban, good], CandidateStatus.downloading), None, [])
    assert st.stage == "Downloading 40%"
    assert st.error is None
    assert st.episodes == []  # one real download left: no breakdown


def test_candidate_failed_only_on_bans_still_says_why() -> None:
    ban = _with_error(_dl(1, "Show FAKE", "failed", 0.0), "removed: torrent only an .exe")
    row = _row(CandidateStatus.failed, dl_state="x", progress=0.0, present=[])
    fields = {f: getattr(row, f) for f in row.__slots__}
    fields.update(dl_state=None, error=None, downloads=(ban,))
    st = _build(_Row(**fields), None, [])
    assert st.stage == "Failed" and st.error == "removed: torrent only an .exe"


def test_cancelled_attempt_does_not_show_as_a_downloading_episode() -> None:
    old = _dl(1, "Show S01E01 720p", "cancelled", 0.2)
    e01 = _dl(2, "Show S01E01 1080p", "downloading", 0.4)
    e02 = _dl(3, "Show S01E02 1080p", "downloading", 0.1)
    st = _build(_season_row([old, e01, e02], CandidateStatus.downloading), None, [])
    assert [(e.label, e.stage) for e in st.episodes] == [
        ("S01E01", "Downloading 40%"),
        ("S01E02", "Downloading 10%"),
    ]


def test_errors_are_aggregated_across_episodes() -> None:
    # The headline follows E01; E02's and E03's failures must not disappear behind it.
    e01 = _dl(1, "Show S01E01 1080p", "downloading", 0.4)
    e02 = _with_error(_dl(2, "Show S01E02 1080p", "failed", 0.0), "no peers")
    e03 = _with_error(_dl(3, "Show S01E03 1080p", "failed", 0.0), "no peers")
    e04 = _with_error(_dl(4, "Show S01E04 1080p", "completed", 1.0), "NAS mount lost")
    st = _build(_season_row([e01, e02, e03, e04], CandidateStatus.downloading), None, [])
    assert st.stage == "Downloading 40%"
    assert st.error == "S01E02, S01E03: no peers\nS01E04: NAS mount lost"


def test_long_error_lists_are_capped() -> None:
    dls = [
        _with_error(_dl(i, f"Show S01E{i:02d} 1080p", "failed", 0.0), f"boom {i}")
        for i in range(1, 9)
    ]
    st = _build(_season_row(dls, CandidateStatus.failed), None, [])
    assert st.error is not None
    assert st.error.count("\n") == 5 and st.error.endswith("…and 3 more")


def test_removal_marker_is_not_shown_as_an_error() -> None:
    from homeTheater.db.models import MARKER_REMOVAL_PENDING

    row = _row(CandidateStatus.failed, dl_state="imported", progress=1.0, present=[])
    fields = {f: getattr(row, f) for f in row.__slots__}
    fields.update(status=CandidateStatus.downloading, error=MARKER_REMOVAL_PENDING)
    assert _build(_Row(**fields), None, []).error is None

    # ...nor on a sibling of something still running, where it's only "imported".
    e01 = _dl(1, "Show S01E01 1080p", "downloading", 0.4)
    e02 = _with_error(_dl(2, "Show S01E02 1080p", "imported", 1.0), MARKER_REMOVAL_PENDING)
    assert _build(_season_row([e01, e02], CandidateStatus.downloading), None, []).error is None


HOLD = (
    "held: 2 video(s) were not imported (a.mkv, b.mkv), so the local copy is kept. "
    "Rename an episode to SxxEyy, or remove what you don't want from the download "
    "folder, and the next sweep finishes this."
)


def test_held_download_says_what_to_do_and_is_not_an_error() -> None:
    row = _row(CandidateStatus.downloading, dl_state="completed", progress=1.0, present=[])
    fields = {f: getattr(row, f) for f in row.__slots__}
    fields["error"] = HOLD
    st = _build(_Row(**fields), None, ["he"])
    assert st.stage == "Held — action needed"
    assert st.error is None
    assert st.attention is not None
    assert st.attention.startswith("2 video(s) were not imported (a.mkv, b.mkv)")
    assert "Rename an episode" in st.attention
    steps = _steps(st)
    assert steps["download"] == "done" and steps["import"] == "held"


def test_held_episode_keeps_its_instruction_behind_a_running_sibling() -> None:
    e01 = _dl(1, "Show S01E01 1080p", "downloading", 0.4)
    e02 = _with_error(_dl(2, "Show S01E02 1080p", "completed", 1.0), HOLD)
    st = _build(_season_row([e01, e02], CandidateStatus.downloading), None, [])
    assert st.stage == "Downloading 40%"
    assert st.attention is not None and st.attention.startswith("S01E02: 2 video(s)")
    assert [(e.stage, e.state) for e in st.episodes] == [
        ("Downloading 40%", "active"),
        ("Held — action needed", "held"),
    ]


def test_import_that_gave_up_offers_a_retry_and_is_not_an_error() -> None:
    dl = _with_error(_dl(1, "Movie.1080p", "completed", 1.0), "needs_attention: disk full")
    row = _row(CandidateStatus.downloading, dl_state="completed", progress=1.0, present=[])
    fields = {f: getattr(row, f) for f in row.__slots__}
    fields.update(error=dl.error, downloads=(dl,))
    st = _build(_Row(**fields), None, [])
    assert st.stage == "Needs attention" and st.error is None
    assert st.attention == "Import gave up: disk full. Fix that, then press Retry import."
    assert st.can_retry_import is True
    assert _steps(st)["import"] == "held"


def test_a_hold_offers_no_retry_button() -> None:
    dl = _with_error(_dl(1, "Movie.1080p", "completed", 1.0), HOLD)
    row = _row(CandidateStatus.downloading, dl_state="completed", progress=1.0, present=[])
    fields = {f: getattr(row, f) for f in row.__slots__}
    fields.update(error=dl.error, downloads=(dl,))
    assert _build(_Row(**fields), None, []).can_retry_import is False


# --- activity(): queries and the live poll ---------------------------------

RPC_URL = "http://localhost:9091/transmission/rpc"


def _torrent_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "t.yaml").write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 't.db'}'}}\n"
        "acquisition: {backend: torrent}\n"
        "subtitles: {languages: [he]}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(tmp_path / "t.yaml"))
    monkeypatch.delenv("TRANSMISSION_URL", raising=False)
    _reset()
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    init_db()
    return get_config()


def _seed_in_flight(count: int, *, downloads: int = 1, first: int = 0) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateSource, Download, OwnedFile, Title

    with session_scope() as s:
        for n in range(first, first + count):
            t = Title(tmdb_id=1000 + n, title=f"Show {n}", kind=TitleKind.series)
            s.add(t)
            s.flush()
            s.add(OwnedFile(path=f"/tv/{n}.mkv", kind=TitleKind.series, title_id=t.id, season=1))
            c = Candidate(
                title_id=t.id,
                season=1,
                source=CandidateSource.discovery,
                status=CandidateStatus.downloading,
            )
            s.add(c)
            s.flush()
            for d in range(downloads):
                s.add(
                    Download(
                        candidate_id=c.id,
                        external_id=f"{n:03d}{d:02d}".ljust(40, "a"),
                        state="downloading",
                        progress=0.1,
                        release=f"Show {n} S01E{d + 1:02d} 1080p",
                    )
                )


def _torrents(hashes: list[str]) -> dict[str, object]:
    return {
        "result": "success",
        "arguments": {
            "torrents": [
                {
                    "hashString": h,
                    "percentDone": 0.5,
                    "status": 4,
                    "rateDownload": 1000,
                    "peersSendingToUs": 3,
                    "eta": 60,
                }
                for h in hashes
            ]
        },
    }


def test_activity_polls_transmission_once_without_file_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import json

    import httpx
    import respx

    from homeTheater.pipeline import activity

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_in_flight(3, downloads=4)

    with respx.mock(assert_all_called=False) as mock:

        def answer(request: httpx.Request) -> httpx.Response:
            ids = json.loads(request.content)["arguments"]["ids"]
            return httpx.Response(200, json=_torrents(ids))

        route = mock.post(RPC_URL).mock(side_effect=answer)
        states = asyncio.run(activity(cfg))

    assert route.call_count == 1
    args = json.loads(route.calls[0].request.content)["arguments"]
    assert len(args["ids"]) == 12
    assert "files" not in args["fields"]
    assert {e.stage for st in states for e in st.episodes} == {"Downloading 50%"}
    assert all(not st.live_down for st in states)


def test_live_poll_keeps_one_session_between_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import httpx
    import respx

    import homeTheater.pipeline as pipeline

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_in_flight(1)
    monkeypatch.setattr(pipeline, "LIVE_TTL_S", 0.0)

    with respx.mock(assert_all_called=False) as mock:
        seen: list[str | None] = []

        def answer(request: httpx.Request) -> httpx.Response:
            sid = request.headers.get("X-Transmission-Session-Id")
            seen.append(sid)
            if sid is None:
                return httpx.Response(409, headers={"X-Transmission-Session-Id": "sid"})
            return httpx.Response(200, json=_torrents(["00000" + "a" * 35]))

        mock.post(RPC_URL).mock(side_effect=answer)

        async def two_polls() -> None:
            await pipeline.activity(cfg)
            await pipeline.activity(cfg)

        asyncio.run(two_polls())

    assert seen == [None, "sid", "sid"]  # negotiated once, not on every poll


def test_tabs_polling_together_share_one_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import httpx
    import respx

    from homeTheater.pipeline import activity

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_in_flight(1)

    with respx.mock(assert_all_called=False) as mock:

        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.1)  # long enough for every tab to arrive mid-poll
            return httpx.Response(200, json=_torrents(["00000" + "a" * 35]))

        route = mock.post(RPC_URL).mock(side_effect=slow)

        async def tabs() -> list[object]:
            together = await asyncio.gather(*(activity(cfg) for _ in range(5)))
            later = await activity(cfg)  # inside the TTL: answered from the cache
            return [*together, later]

        results = asyncio.run(tabs())

    assert route.call_count == 1
    assert all(r[0].down_rate == 1000 for r in results)  # type: ignore[index]


def test_a_tab_that_goes_away_does_not_cancel_the_shared_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import httpx
    import respx

    from homeTheater.pipeline import activity

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_in_flight(1)

    with respx.mock(assert_all_called=False) as mock:

        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.2)
            return httpx.Response(200, json=_torrents(["00000" + "a" * 35]))

        mock.post(RPC_URL).mock(side_effect=slow)

        async def scene() -> list[object]:
            leaver = asyncio.create_task(activity(cfg))
            stayer = asyncio.create_task(activity(cfg))
            await asyncio.sleep(0.1)
            leaver.cancel()
            return await stayer

        states = asyncio.run(scene())

    assert states[0].down_rate == 1000  # type: ignore[attr-defined]
    assert states[0].live_down is False  # type: ignore[attr-defined]


def test_hung_transmission_degrades_within_the_limit_and_is_not_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import time

    import httpx
    import respx

    import homeTheater.pipeline as pipeline

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_in_flight(2)
    monkeypatch.setattr(pipeline, "LIVE_TIMEOUT_S", 0.05)

    with respx.mock(assert_all_called=False) as mock:

        calls: list[int] = []

        async def hang(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            await asyncio.sleep(30)
            return httpx.Response(200, json=_torrents([]))

        mock.post(RPC_URL).mock(side_effect=hang)

        async def two_polls() -> tuple[list[object], float]:
            start = time.monotonic()
            first = await pipeline.activity(cfg)
            await pipeline.activity(cfg)
            return first, time.monotonic() - start

        states, elapsed = asyncio.run(two_polls())

    assert elapsed < 2.0
    assert len(calls) == 1  # the second poll used the "it's down" answer
    assert all(st.live_down for st in states)  # type: ignore[attr-defined]
    assert states[0].stage == "Downloading 10%"  # type: ignore[attr-defined]


def test_transmission_refusing_connections_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import httpx
    import respx

    from homeTheater.pipeline import activity

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_in_flight(1)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(RPC_URL).mock(side_effect=httpx.ConnectError("refused"))
        states = asyncio.run(activity(cfg))

    assert states[0].live_down is True
    assert states[0].error is None  # an outage isn't this item's error


def test_item_with_nothing_transferring_is_not_flagged_during_an_outage() -> None:
    st = _build(
        _season_row([_dl(1, "X S01E01", "completed", 1.0)], CandidateStatus.downloading),
        None,
        [],
        None,
        True,
    )
    assert st.live_down is False


def test_unknown_hash_is_not_an_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import httpx
    import respx

    from homeTheater.pipeline import activity

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_in_flight(1)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(RPC_URL).mock(return_value=httpx.Response(200, json=_torrents([])))
        states = asyncio.run(activity(cfg))

    assert states[0].live_down is False and states[0].down_rate is None


def test_activity_query_count_does_not_grow_with_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import event

    from homeTheater.db.session import get_engine
    from homeTheater.pipeline import _collect

    cfg = _torrent_config(tmp_path, monkeypatch)

    def count() -> int:
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_rest) -> None:  # type: ignore[no-untyped-def]
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        engine = get_engine()
        event.listen(engine, "before_cursor_execute", record)
        try:
            _collect(cfg)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    _seed_in_flight(2, downloads=2)
    few = count()
    _seed_in_flight(20, downloads=2, first=100)
    assert count() == few


# --- subtitles are the candidate's own -------------------------------------


def _seed_season_files(season: int, files: list[tuple[int, list[str]]]) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateSource, OwnedFile, Title

    with session_scope() as s:
        t = Title(tmdb_id=77, title="Severance", kind=TitleKind.series)
        s.add(t)
        s.flush()
        for i, (file_season, langs) in enumerate(files):
            s.add(
                OwnedFile(
                    title_id=t.id,
                    path=f"/tv/{i}.mkv",
                    kind=TitleKind.series,
                    season=file_season,
                    subtitle_langs=langs,
                )
            )
        s.add(
            Candidate(
                title_id=t.id,
                season=season,
                source=CandidateSource.discovery,
                status=CandidateStatus.downloading,
            )
        )


def test_other_seasons_subtitles_do_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.pipeline import _collect

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_season_files(3, [(1, ["he"]), (2, ["he"])])
    assert _collect(cfg)[0].subtitle_present == []


def test_a_language_counts_once_the_whole_season_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.pipeline import _collect

    cfg = _torrent_config(tmp_path, monkeypatch)
    _seed_season_files(3, [(3, ["he", "en"]), (3, ["en"])])
    assert _collect(cfg)[0].subtitle_present == ["en"]


def test_activity_payload_carries_the_new_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, Download, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=3, title="Held Movie", kind=TitleKind.movie)
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id, source=CandidateSource.discovery, status=CandidateStatus.downloading
        )
        s.add(c)
        s.flush()
        s.add(
            Download(
                candidate_id=c.id, external_id="h", state="completed", progress=1.0, error=HOLD
            )
        )

    with TestClient(create_app()) as client:
        item = client.get("/api/activity").json()["items"][0]
    assert item["stage"] == "Held — action needed"
    assert item["error"] is None and "Rename an episode" in item["attention"]
    assert item["live_down"] is False
