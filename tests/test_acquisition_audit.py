"""Regression tests for the November 2026 audit's acquisition findings.

Each test names the audit id it holds down. Transmission is a small in-memory fake
keeping real torrent state, so tests look at what ended up in the client, and
indexers are mocked with respx. Nothing here touches a network, a NAS or a real
Transmission.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from homeTheater.acquisition.torrent import importer
from homeTheater.acquisition.torrent import service as svc
from homeTheater.acquisition.torrent.base import TorrentRelease
from homeTheater.acquisition.torrent.select import (
    build_query,
    is_season_pack,
    search_title,
    select_release,
)
from homeTheater.db.base import utcnow
from homeTheater.db.models import (
    MARKER_NEEDS_ATTENTION,
    MARKER_REMOVED,
    MARKER_STALLED,
    Candidate,
    CandidateSource,
    CandidateStatus,
    Download,
    DownloadTracking,
    GrabRetry,
    OwnedFile,
    Title,
    TitleKind,
)

APIBAY = "http://apibay.test"
TRANSMISSION = "http://transmission.test/transmission/rpc"
HASH = "a" * 40
OTHER = "b" * 40
GB = 1_500_000_000


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    path = tmp_path / "torrent.yaml"
    path.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'torrent.db'}'}}\n"
        "features: {dry_run: false, auto_approve: false}\n"
        "acquisition: {backend: torrent}\n"
        "torrent:\n"
        "  enabled_sources: [piratebay]\n"
        "  min_seeders: 1\n"
        f"  piratebay_api_url: {APIBAY}\n"
        f"  library_base_dir: {tmp_path / 'lib'}\n"
        "  delete_local_after_import: true\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(path))
    monkeypatch.setenv("TRANSMISSION_URL", TRANSMISSION)
    _reset()
    init_db()
    monkeypatch.setattr(svc, "_METADATA_PROBE_DELAY", 0.0)
    yield get_config()
    _reset()


class Client:
    """Transmission, as far as these tests care."""

    def __init__(self, torrents: dict[str, dict] | None = None) -> None:
        self.torrents = torrents or {}
        self.calls: list[str] = []
        self.fail_adds_after: int | None = None
        self.on_add: Any = None

    def add(self, infohash: str = HASH, **fields: Any) -> dict:
        self.torrents[infohash] = {
            "hashString": infohash,
            "name": "x",
            "percentDone": 0.0,
            "status": 4,
            "downloadDir": "/d",
            "error": 0,
            "errorString": "",
            **fields,
        }
        return self.torrents[infohash]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, args = body["method"], body["arguments"]
        self.calls.append(method)
        result: dict[str, Any] = {}
        if method == "torrent-add":
            adds = self.calls.count("torrent-add")
            if self.fail_adds_after is not None and adds > self.fail_adds_after:
                return httpx.Response(500)
            btih = args["filename"].split("btih:")[1].split("&")[0].lower()
            existed = btih in self.torrents
            self.torrents.setdefault(btih, self.add(btih, files=[{"name": "x.mkv"}]))
            result = {
                ("torrent-duplicate" if existed else "torrent-added"): {
                    "hashString": btih,
                    "name": "x",
                }
            }
            if self.on_add is not None:
                self.on_add()
        elif method == "torrent-get":
            ids = args.get("ids")
            result = {
                "torrents": [t for h, t in self.torrents.items() if ids is None or h in ids],
            }
        elif method == "torrent-remove":
            for infohash in args["ids"]:
                self.torrents.pop(infohash, None)
        return httpx.Response(200, json={"result": "success", "arguments": result})


def _client(**torrents: Any) -> Client:
    fake = Client()
    respx.post(TRANSMISSION).mock(side_effect=fake)
    return fake


def _row(name: str, infohash: str = HASH, seeders: int = 100, size: int = GB) -> dict:
    return {
        "id": "1",
        "name": name,
        "info_hash": infohash,
        "seeders": str(seeders),
        "leechers": "1",
        "size": str(size),
    }


def _indexer(by_query: dict[str, list[dict]]) -> list[str]:
    asked: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        asked.append(query)
        return httpx.Response(200, json=by_query.get(query, []))

    respx.get(f"{APIBAY}/q.php").mock(side_effect=respond)
    return asked


def _seed(
    *,
    title: str = "The Matrix",
    year: int | None = 1999,
    kind: TitleKind = TitleKind.movie,
    season: int | None = None,
    status: CandidateStatus = CandidateStatus.approved,
    target: int | None = None,
    downloads: list[tuple[str, str, str | None]] | None = None,
    series_status: str | None = None,
) -> int:
    """A candidate with (hash, state, error) download rows; returns its id."""

    from homeTheater.db import session_scope

    with session_scope() as s:
        t = s.query(Title).filter_by(title=title).one_or_none()
        if t is None:
            t = Title(
                tmdb_id=603 + (s.query(Title).count() * 7),
                title=title,
                year=year,
                kind=kind,
                series_status=series_status,
            )
            s.add(t)
            s.flush()
        c = Candidate(
            title_id=t.id,
            season=season,
            source=CandidateSource.discovery,
            status=status,
            features={"season_episodes": target} if target else None,
        )
        s.add(c)
        s.flush()
        for n, (infohash, state, error) in enumerate(downloads or [], start=1):
            release = f"{title} S{season or 1:02d}E{n:02d} 1080p" if season else title
            s.add(
                Download(
                    candidate_id=c.id,
                    external_id=infohash,
                    state=state,
                    error=error,
                    release=release,
                    progress=0.0,
                )
            )
        return c.id


def _status(cid: int) -> CandidateStatus:
    from homeTheater.db import session_scope

    with session_scope() as s:
        return s.get(Candidate, cid).status


def _rows(cid: int) -> list[tuple[str | None, str | None, str | None]]:
    from homeTheater.db import session_scope

    with session_scope() as s:
        return [
            (d.external_id, d.state, d.error)
            for d in s.query(Download).filter_by(candidate_id=cid).order_by(Download.id)
        ]


def _age(cid: int, hours: float) -> None:
    """Pretend a candidate's downloads were grabbed ``hours`` ago."""

    from homeTheater.db import session_scope

    with session_scope() as s:
        for d in s.query(Download).filter_by(candidate_id=cid):
            d.created_at = utcnow() - timedelta(hours=hours)


def _rewind_clock(cid: int, hours: float) -> None:
    from homeTheater.db import session_scope

    with session_scope() as s:
        for d in s.query(Download).filter_by(candidate_id=cid):
            track = s.get(DownloadTracking, d.id)
            track.last_progress_at = utcnow() - timedelta(hours=hours)


async def _sync(cfg: Any) -> Any:
    from homeTheater.acquisition import sync_downloads

    return await sync_downloads(cfg)


async def _queue(cfg: Any, cid: int) -> Any:
    from homeTheater.acquisition import queue_candidate

    return await queue_candidate(cfg, cid)


# --- A4: a re-grab must never strand the candidate ----------------------------


@respx.mock
@pytest.mark.parametrize("old_state", ["failed", "cancelled"])
async def test_a4_regrab_of_a_dead_row_reactivates_it(cfg: Any, old_state: str) -> None:
    cid = _seed(downloads=[(HASH, old_state, "stalled: gone")], status=CandidateStatus.failed)
    _indexer({"The Matrix 1999": [_row("The Matrix 1999 1080p WEB")]})
    _client()

    outcome = await _queue(cfg, cid)

    assert outcome.queued
    assert _rows(cid) == [(HASH, "downloading", None)]
    assert _status(cid) == CandidateStatus.queued


def test_a4_regrab_of_a_lost_file_reactivates_the_imported_row(cfg: Any) -> None:
    """Restart keeps imported rows, and a file can vanish from the NAS later: the same
    release has to stay grabbable, or the only seeded copy is banned for good."""
    cid = _seed(downloads=[(HASH, "imported", None)])

    svc._record_grabs(cid, [(HASH, "x", False)])

    assert _rows(cid) == [(HASH, "downloading", None)]
    assert _status(cid) == CandidateStatus.queued


def test_a4_a_stop_never_rewrites_imported_history(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.cancelled, downloads=[(HASH, "imported", None)])

    assert svc._record_grabs(cid, [(HASH, "x", False)]) == [HASH]

    assert _rows(cid) == [(HASH, "imported", None)]


def test_a4_a_ban_row_is_not_reactivated(cfg: Any) -> None:
    ban = f"{MARKER_REMOVED} executable"
    cid = _seed(downloads=[(HASH, "failed", ban)])

    svc._record_grabs(cid, [(HASH, "x", False)])

    assert _rows(cid) == [(HASH, "failed", ban)]
    assert _status(cid) == CandidateStatus.approved  # no live row, so no "queued"


@respx.mock
async def test_a4_sweep_settles_movie_marked_in_flight_with_no_live_rows(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.queued, downloads=[(HASH, "failed", "x")])
    _client()

    await _sync(cfg)

    assert _status(cid) == CandidateStatus.failed


@respx.mock
async def test_a4_sweep_returns_stranded_season_to_top_up(cfg: Any) -> None:
    cid = _seed(
        title="Silo",
        kind=TitleKind.series,
        season=2,
        target=10,
        status=CandidateStatus.downloading,
    )
    _client()

    await _sync(cfg)

    assert _status(cid) == CandidateStatus.approved


@respx.mock
async def test_a4_sweep_leaves_a_grab_in_progress_alone(cfg: Any) -> None:
    """Its claim has no rows yet, and the acquire lock says it's still running."""
    from homeTheater.locks import job_lock

    cid = _seed(status=CandidateStatus.queued)
    _client()

    with job_lock(cfg, "acquire"):
        await _sync(cfg)

    assert _status(cid) == CandidateStatus.queued


# --- A5 / L11 / W1: a rejection during a grab stands ---------------------------


@respx.mock
@pytest.mark.parametrize("stopped", [CandidateStatus.rejected, CandidateStatus.cancelled])
async def test_a5_candidate_stopped_mid_grab_keeps_its_status(
    cfg: Any, stopped: CandidateStatus
) -> None:
    from homeTheater.db import session_scope

    cid = _seed()
    _indexer({"The Matrix 1999": [_row("The Matrix 1999 1080p WEB")]})
    fake = _client()

    def stop() -> None:
        with session_scope() as s:
            s.get(Candidate, cid).status = stopped

    fake.on_add = stop

    outcome = await _queue(cfg, cid)

    assert not outcome.queued
    assert _status(cid) == stopped
    assert _rows(cid) == [(HASH, "cancelled", None)]
    assert HASH not in fake.torrents  # no torrent left downloading for nobody


@respx.mock
async def test_a5_stop_mid_grab_halts_the_remaining_adds(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(title="Silo", kind=TitleKind.series, season=2, target=3)
    _indexer({f"Silo S02E0{e}": [_row(f"Silo S02E0{e} 1080p WEB", str(e) * 40)] for e in (1, 2, 3)})
    fake = _client()

    def stop() -> None:
        with session_scope() as s:
            s.get(Candidate, cid).status = CandidateStatus.cancelled

    fake.on_add = stop

    await _queue(cfg, cid)

    assert fake.calls.count("torrent-add") == 1
    assert fake.torrents == {}


def test_a5_record_grabs_rereads_the_candidate(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.rejected)

    to_remove = svc._record_grabs(cid, [(HASH, "x", False)])

    assert to_remove == [HASH]
    assert _status(cid) == CandidateStatus.rejected
    assert _rows(cid) == [(HASH, "cancelled", None)]


@respx.mock
async def test_w1_arr_rejection_during_the_add_stands(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.acquisition import service
    from homeTheater.acquisition.base import AddResult
    from homeTheater.db import session_scope

    monkeypatch.setattr(service, "_client_for", lambda *_a: arr)
    arr_cfg = cfg.model_copy(
        update={"acquisition": cfg.acquisition.model_copy(update={"backend": "arr"})}
    )
    cid = _seed()

    class Arr:
        async def add(self, external_id: int, **_kw: Any) -> AddResult:
            with session_scope() as s:
                s.get(Candidate, cid).status = CandidateStatus.rejected
            return AddResult(external_id, "x")

    arr = Arr()

    outcome = await _queue(arr_cfg, cid)

    assert not outcome.queued
    assert _status(cid) == CandidateStatus.rejected
    assert [state for _h, state, _e in _rows(cid)] == ["cancelled"]


# --- A6: series grabs check the show ------------------------------------------


def test_a6_series_grab_checks_the_show_name() -> None:
    wrong = TorrentRelease("pb", "Silo Stories S03 1080p WEB", 900, 1, None, infohash=HASH)
    right = TorrentRelease("pb", "Silo S03 1080p WEB", 20, 1, None, infohash=OTHER)
    kept = TorrentRelease("pb", "The Office S03 1080p WEB", 900, 1, None, infohash="c" * 40)

    picked = select_release(
        [wrong, kept, right],
        allowed_resolutions=["1080p"],
        min_seeders=1,
        season=3,
        expected_title="Silo",
    )

    assert picked is right


@respx.mock
async def test_a6_queue_season_passes_the_title(cfg: Any) -> None:
    cid = _seed(title="Silo", kind=TitleKind.series, season=3, target=8)
    _indexer({"Silo S03": [_row("Another Show S03 1080p WEB")]})
    _client()

    outcome = await _queue(cfg, cid)

    assert not outcome.queued


@respx.mock
async def test_a6_episode_probe_passes_the_title_too(cfg: Any) -> None:
    cid = _seed(title="Silo", kind=TitleKind.series, season=3, target=1)
    _indexer({"Silo S03E01": [_row("Silo Stories S03E01 1080p WEB")]})
    fake = _client()

    outcome = await _queue(cfg, cid)

    assert not outcome.queued
    assert "torrent-add" not in fake.calls


@respx.mock
async def test_a6_series_with_no_season_data_is_refused(cfg: Any) -> None:
    cid = _seed(title="Silo", kind=TitleKind.series)
    asked = _indexer({"Silo": [_row("Silo 1080p WEB")]})
    fake = _client()

    outcome = await _queue(cfg, cid)

    assert not outcome.queued and "no season data" in outcome.message
    assert asked == [] and fake.calls == []
    assert _status(cid) == CandidateStatus.approved


# --- A8: movie matching -------------------------------------------------------


def _pick(name: str, title: str, year: int | None) -> TorrentRelease | None:
    rel = TorrentRelease("pb", name, 100, 1, None, infohash=HASH)
    return select_release(
        [rel],
        allowed_resolutions=["1080p"],
        min_seeders=1,
        movie=True,
        expected_title=title,
        expected_year=year,
    )


@pytest.mark.parametrize(
    ("name", "title", "year"),
    [
        ("Amelie 2001 1080p BluRay x264", "Amélie", 2001),
        ("Amélie 2001 1080p BluRay x264", "Amélie", 2001),
        ("Fast and Furious 2009 1080p BluRay", "Fast & Furious", 2009),
        ("Fast & Furious 2009 1080p BluRay", "Fast and Furious", 2009),
        ("Cam 2018 1080p WEB-DL x264", "Cam", 2018),
        ("Widows Bay 2026 1080p WEB", "Widow's Bay", 2026),
        ("The Matrix 1999 Extended Cut 1080p", "The Matrix", 1999),
        ("Spider-Man No Way Home 2021 1080p", "Spider-Man: No Way Home", 2021),
        ("Spiderman No Way Home 2021 1080p", "Spider-Man: No Way Home", 2021),
        ("Blade.Runner.2049.2017.1080p.BluRay", "Blade Runner 2049", 2017),
        ("1917.2019.1080p.WEB", "1917", 2019),
        ("Digger 2025 1080p WEB", "Digger", 2026),  # one year off is still allowed
        ("Digger 1080p WEB", "Digger", 2026),  # no year: allowed, ranked last
    ],
)
def test_a8_real_matches(name: str, title: str, year: int) -> None:
    assert _pick(name, title, year) is not None


@pytest.mark.parametrize(
    ("name", "title", "year"),
    [
        ("The Matrix Reloaded 2003 1080p", "The Matrix", None),
        ("The Matrix Reloaded 1999 1080p", "The Matrix", 1999),
        ("Alien 3 1992 1080p", "Alien", 1992),
        ("Dune Part Two 2024 1080p", "Dune", 2024),
        ("Cam Girl 2018 1080p", "Cam", 2018),
        ("Gold Digger 2026 1080p", "Digger", 2026),
        ("Digger Knows Best 2026 1080p", "Digger", 2026),
        ("Digger 2020 1080p AMZN WEB-DL", "Digger", 2026),
        ("Fast Furious 2009 1080p", "Fast & Furious", 2009),
    ],
)
def test_a8_near_misses_are_refused(name: str, title: str, year: int | None) -> None:
    assert _pick(name, title, year) is None


def test_a8_a_real_quality_tag_still_counts_after_the_title() -> None:
    assert _pick("Cam 2018 CAM 1080p", "Cam", 2018) is None
    assert _pick("The Matrix 1999 HDCAM 1080p", "The Matrix", 1999) is None


def test_a8_yearless_loses_to_any_dated_release() -> None:
    yearless = TorrentRelease("pb", "Digger 1080p WEB", 9000, 1, None, infohash=HASH)
    off_by_one = TorrentRelease("pb", "Digger 2025 720p WEB", 50, 1, None, infohash=OTHER)
    exact = TorrentRelease("pb", "Digger 2026 720p WEB", 5, 1, None, infohash="c" * 40)
    args: dict[str, Any] = {
        "allowed_resolutions": ["1080p", "720p"],
        "min_seeders": 1,
        "movie": True,
        "expected_title": "Digger",
        "expected_year": 2026,
    }

    assert select_release([yearless, off_by_one, exact], **args) is exact
    assert select_release([yearless, off_by_one], **args) is off_by_one
    assert select_release([yearless], **args) is yearless


# --- A9: progress, not creation time ------------------------------------------


@respx.mock
async def test_a9_partial_torrent_with_no_progress_fails(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    fake = _client()
    fake.add(percentDone=0.4)
    await _sync(cfg)
    assert _rows(cid)[0][1] == "downloading"

    _rewind_clock(cid, cfg.torrent.stale_after_hours + 1)
    stats = await _sync(cfg)

    assert stats.failed == 1
    infohash, state, error = _rows(cid)[0]
    assert state == "failed" and error.startswith(MARKER_STALLED)
    assert _status(cid) == CandidateStatus.failed


@respx.mock
async def test_a9_any_progress_restarts_the_clock(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    fake = _client()
    torrent = fake.add(percentDone=0.4)
    await _sync(cfg)
    _rewind_clock(cid, cfg.torrent.stale_after_hours + 1)
    torrent["percentDone"] = 0.41

    await _sync(cfg)

    assert _rows(cid)[0][1] == "downloading"


@respx.mock
async def test_a9_old_grab_still_moving_is_not_stale(cfg: Any) -> None:
    """The clock is progress, not age: a three-day download isn't a dead one."""
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    _age(cid, 72)
    fake = _client()
    fake.add(percentDone=0.4)

    await _sync(cfg)

    assert _rows(cid)[0][1] == "downloading"


@respx.mock
async def test_a9_sleeping_host_is_not_a_stall(cfg: Any) -> None:
    """The Mac slept for ten hours and Transmission with it: no bytes, no fault."""
    from homeTheater.db import session_scope

    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    fake = _client()
    fake.add(percentDone=0.4)
    await _sync(cfg)
    with session_scope() as s:
        for track in s.query(DownloadTracking):
            track.last_progress_at = utcnow() - timedelta(hours=10)
            track.last_checked_at = utcnow() - timedelta(hours=10)

    await _sync(cfg)

    assert _rows(cid)[0][1] == "downloading"


@respx.mock
async def test_a9_local_client_error_fails_at_once(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    fake = _client()
    fake.add(percentDone=0.4, error=3, errorString="No space left on device", status=0)

    await _sync(cfg)

    _h, state, error = _rows(cid)[0]
    assert state == "failed" and "No space left" in error


@respx.mock
@pytest.mark.parametrize("code", [1, 2])
async def test_a9_tracker_trouble_is_not_fatal(cfg: Any, code: int) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    fake = _client()
    fake.add(percentDone=0.4, error=code, errorString="tracker timeout")

    await _sync(cfg)

    assert _rows(cid)[0][1] == "downloading"


@respx.mock
async def test_a9_stopped_torrent_is_not_downloading(cfg: Any) -> None:
    from homeTheater.acquisition.torrent.transmission import TransmissionClient

    fake = _client()
    fake.add(percentDone=0.4, status=0)
    async with httpx.AsyncClient() as http:
        st = await TransmissionClient(TRANSMISSION, http).status(HASH)

    assert st is not None and not st.downloading and not st.complete


@respx.mock
async def test_a9_hand_paused_torrent_is_never_failed_or_removed(cfg: Any) -> None:
    """A pause is the owner's decision: however long it lasts, the data stays."""

    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    fake = _client()
    fake.add(percentDone=0.4, status=0)
    await _sync(cfg)

    _rewind_clock(cid, cfg.torrent.stale_after_hours * 10)
    stats = await _sync(cfg)

    assert stats.failed == 0
    assert _rows(cid)[0][1] == "downloading"
    assert HASH in fake.torrents


# --- A10 / A11: dead episodes and dead hashes ---------------------------------


@respx.mock
async def test_a10_dead_last_episode_returns_season_to_approved(cfg: Any) -> None:
    from homeTheater.acquisition import queue_approved
    from homeTheater.db import session_scope

    cid = _seed(
        title="Silo",
        kind=TitleKind.series,
        season=2,
        target=10,
        status=CandidateStatus.downloading,
        downloads=[(HASH, "downloading", None)],
    )
    _age(cid, cfg.torrent.stale_after_hours + 1)
    _client()

    await _sync(cfg)

    assert _status(cid) == CandidateStatus.approved
    with session_scope() as s:
        retry = s.get(GrabRetry, cid)
        assert retry.attempts == 1
    stats = await queue_approved(cfg)
    assert stats.considered == 0  # the scheduled path waits out the backoff


@respx.mock
async def test_a10_retries_run_out_and_it_shows_failed(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(title="Silo", kind=TitleKind.series, season=2, target=10)
    _client()
    with session_scope() as s:
        s.add(GrabRetry(candidate_id=cid, attempts=svc._GRAB_RETRY_MAX, next_attempt_at=utcnow()))
        cand = s.get(Candidate, cid)
        svc._settle_after_loss(s, cand, 0)

    assert _status(cid) == CandidateStatus.failed


def test_a10_backoff_grows_and_has_a_ceiling(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(title="Silo", kind=TitleKind.series, season=2, target=10)
    gaps = []
    for _ in range(svc._GRAB_RETRY_MAX):
        with session_scope() as s:
            svc._settle_after_loss(s, s.get(Candidate, cid), 0)
        with session_scope() as s:
            gaps.append((s.get(GrabRetry, cid).next_attempt_at, utcnow()))
    assert _status(cid) == CandidateStatus.approved
    waits = [
        round((a.replace(tzinfo=None) - b.replace(tzinfo=None)).total_seconds()) for a, b in gaps
    ]
    assert waits == sorted(waits)
    assert waits[0] == pytest.approx(3600, abs=2)
    assert waits[-1] <= svc._GRAB_RETRY_BACKOFF_MAX.total_seconds() + 2


@respx.mock
async def test_a10_a_lost_episode_already_owned_completes_the_season(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(
        title="Silo",
        kind=TitleKind.series,
        season=2,
        target=1,
        status=CandidateStatus.downloading,
        downloads=[(HASH, "downloading", None)],
    )
    _age(cid, cfg.torrent.stale_after_hours + 1)
    with session_scope() as s:
        title = s.query(Title).one()
        s.add(OwnedFile(path="/x", title_id=title.id, kind=TitleKind.series, season=2, episode=1))
    _client()

    await _sync(cfg)

    assert _status(cid) == CandidateStatus.imported


@respx.mock
async def test_a11_stale_torrent_leaves_the_client(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    _age(cid, cfg.torrent.stale_after_hours + 1)
    fake = _client()
    fake.add()

    await _sync(cfg)

    assert _rows(cid)[0][1] == "failed"
    assert HASH not in fake.torrents


@respx.mock
async def test_a11_stale_torrent_shared_with_a_live_row_stays(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    _age(cid, cfg.torrent.stale_after_hours + 1)
    _seed(title="Other", status=CandidateStatus.downloading, downloads=[(HASH, "completed", None)])
    fake = _client()
    fake.add()

    await _sync(cfg)

    assert HASH in fake.torrents


@respx.mock
async def test_a11_time_queued_in_the_client_does_not_count(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    _age(cid, cfg.torrent.stale_after_hours + 1)
    fake = _client()
    fake.add(status=3)  # waiting for a download slot

    await _sync(cfg)

    assert _rows(cid)[0][1] == "downloading"
    assert HASH in fake.torrents


def test_a11_dead_hash_ranks_last() -> None:
    dead = TorrentRelease("pb", "M 2020 1080p WEB", 900, 1, None, infohash=HASH)
    alive = TorrentRelease("pb", "M 2020 720p WEB", 5, 1, None, infohash=OTHER)
    args: dict[str, Any] = {"allowed_resolutions": ["1080p", "720p"], "min_seeders": 1}

    assert select_release([dead, alive], dead=frozenset({HASH}), **args) is alive
    assert select_release([dead], dead=frozenset({HASH}), **args) is dead  # better than nothing


def test_a11_dead_hashes_come_from_stalled_rows_only(cfg: Any) -> None:
    _seed(downloads=[(HASH, "failed", f"{MARKER_STALLED} x"), (OTHER, "failed", "client error")])

    assert svc._dead_hashes() == frozenset({HASH})


# --- A12: episode probing -----------------------------------------------------


@respx.mock
async def test_a12_known_target_probes_past_a_gap(cfg: Any) -> None:
    cid = _seed(title="Silo", kind=TitleKind.series, season=2, target=6)
    _indexer(
        {
            "Silo S02E01": [_row("Silo S02E01 1080p WEB", "1" * 40)],
            "Silo S02E06": [_row("Silo S02E06 1080p WEB", "6" * 40)],
        }
    )
    _client()

    outcome = await _queue(cfg, cid)

    assert outcome.queued and "E06" in outcome.message


@respx.mock
async def test_a12_unknown_target_still_stops_after_misses(cfg: Any) -> None:
    cid = _seed(title="Silo", kind=TitleKind.series, season=2)
    asked = _indexer({"Silo S02E01": [_row("Silo S02E01 1080p WEB", "1" * 40)]})
    _client()

    await _queue(cfg, cid)

    assert "Silo S02E02" in asked and "Silo S02E03" in asked and "Silo S02E04" not in asked


@respx.mock
async def test_a12_every_query_strips_apostrophes(cfg: Any) -> None:
    cid = _seed(title="Widow's Bay", kind=TitleKind.series, season=1, target=2)
    asked = _indexer({})
    _client()

    await _queue(cfg, cid)

    assert asked and not [q for q in asked if "'" in q]
    assert "Widows Bay Season 1" in asked and "Widows Bay S01E02" in asked


def test_a12_one_sanitiser() -> None:
    assert search_title("Widow’s Bay") == "Widows Bay"
    assert build_query("Widow's Bay", None, TitleKind.series, 1, episode=2) == "Widows Bay S01E02"


# --- A13: what counts as a pack -----------------------------------------------


@pytest.mark.parametrize(
    ("name", "pack"),
    [
        ("Show S02 1080p WEB", True),
        ("Show Season 2 Complete 1080p", True),
        ("Show S02 Part 1 1080p WEB", False),
        ("Show S02 Disc 2 1080p", False),
        ("Show S02E01 1080p", False),
    ],
)
def test_a13_is_season_pack(name: str, pack: bool) -> None:
    assert is_season_pack(name) is pack


def test_a13_a_slice_of_a_season_is_not_selected_as_the_pack() -> None:
    part = TorrentRelease("pb", "Silo S02 Part 1 1080p WEB", 900, 1, None, infohash=HASH)
    pack = TorrentRelease("pb", "Silo S02 1080p WEB", 5, 1, None, infohash=OTHER)
    args: dict[str, Any] = {"allowed_resolutions": ["1080p"], "min_seeders": 1, "season": 2}

    assert select_release([part, pack], **args) is pack
    assert select_release([part], **args) is None


def test_a13_a_live_slice_does_not_read_as_a_live_pack(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(title="Silo", kind=TitleKind.series, season=2)
    with session_scope() as s:
        title_id = s.query(Title).one().id
        s.add(
            Download(
                candidate_id=cid, external_id=HASH, state="downloading", release="Silo S02 Part 1"
            )
        )

    assert svc._grabbed_episodes(cid, title_id, 2) == set()


def test_a13_coverage_ignores_an_imported_slice(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(title="Silo", kind=TitleKind.series, season=2)
    with session_scope() as s:
        s.add(
            Download(
                candidate_id=cid, external_id=HASH, state="imported", release="Silo S02 Part 1"
            )
        )
        assert svc._coverage_from_releases(s, cid) == (set(), False)


# --- A14 / W5: one grab at a time ---------------------------------------------


def test_a14_claim_is_atomic(cfg: Any) -> None:
    from homeTheater.acquisition.service import claim_candidate

    cid = _seed()

    assert [claim_candidate(cid) for _ in range(3)] == [True, False, False]


def test_a14_claim_refuses_stopped_candidates(cfg: Any) -> None:
    from homeTheater.acquisition.service import claim_candidate

    assert not claim_candidate(_seed(status=CandidateStatus.rejected))
    assert not claim_candidate(_seed(title="B", status=CandidateStatus.cancelled))


@respx.mock
async def test_a14_grab_that_finds_nothing_gives_the_claim_back(cfg: Any) -> None:
    cid = _seed()
    _indexer({})
    _client()

    outcome = await _queue(cfg, cid)

    assert not outcome.queued
    assert _status(cid) == CandidateStatus.approved


@respx.mock
async def test_a14_grab_that_raises_gives_the_claim_back(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _seed(status=CandidateStatus.failed)
    _indexer({"The Matrix 1999": [_row("The Matrix 1999 1080p WEB")]})
    fake = _client()
    fake.fail_adds_after = 0

    with pytest.raises(httpx.HTTPError):
        await _queue(cfg, cid)

    assert _status(cid) == CandidateStatus.failed


@respx.mock
async def test_a14_each_row_is_recorded_right_after_its_add(cfg: Any) -> None:
    """The third add dies. The first two torrents are in the client, so they have to
    be on record too, or nothing would ever track or clean them up."""
    cid = _seed(title="Silo", kind=TitleKind.series, season=2, target=3)
    _indexer({f"Silo S02E0{e}": [_row(f"Silo S02E0{e} 1080p WEB", str(e) * 40)] for e in (1, 2, 3)})
    fake = _client()
    fake.fail_adds_after = 2

    with pytest.raises(httpx.HTTPError):
        await _queue(cfg, cid)

    assert [h for h, _s, _e in _rows(cid)] == ["1" * 40, "2" * 40]
    assert _status(cid) == CandidateStatus.queued


def test_a14_one_row_per_candidate_and_hash(cfg: Any) -> None:
    from sqlalchemy.exc import IntegrityError

    from homeTheater.db import session_scope

    cid = _seed(downloads=[(HASH, "failed", None)])

    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(Download(candidate_id=cid, external_id=HASH, state="failed"))


def test_a14_rows_without_a_handle_may_repeat(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(downloads=[(None, "failed", None), (None, "failed", None)])  # type: ignore[list-item]

    with session_scope() as s:
        assert s.query(Download).filter_by(candidate_id=cid).count() == 2


async def test_a14_manual_grab_respects_the_acquire_lock(cfg: Any) -> None:
    from homeTheater.errors import JobBusyError
    from homeTheater.locks import job_lock

    cid = _seed()

    with job_lock(cfg, "acquire"), pytest.raises(JobBusyError):
        await _queue(cfg, cid)
    assert _status(cid) == CandidateStatus.approved


async def test_a14_restart_respects_the_acquire_lock(cfg: Any) -> None:
    from homeTheater.acquisition import restart_candidate
    from homeTheater.errors import JobBusyError
    from homeTheater.locks import job_lock

    cid = _seed(status=CandidateStatus.failed, downloads=[(HASH, "failed", None)])

    with job_lock(cfg, "acquire"), pytest.raises(JobBusyError):
        await restart_candidate(cfg, cid)
    assert _rows(cid) == [(HASH, "failed", None)]  # nothing wiped


# --- A15 / W9: cancelled, not rejected ----------------------------------------


@respx.mock
async def test_a15_cancel_touches_only_live_rows(cfg: Any) -> None:
    from homeTheater.acquisition import cancel_candidate

    ban = f"{MARKER_REMOVED} junk"
    cid = _seed(
        title="Silo",
        kind=TitleKind.series,
        season=1,
        status=CandidateStatus.downloading,
        downloads=[
            ("1" * 40, "imported", None),
            ("2" * 40, "downloading", None),
            ("3" * 40, "failed", ban),
            ("4" * 40, "completed", "held: x"),
        ],
    )
    fake = _client()
    for infohash in ("1", "2", "4"):
        fake.add(infohash * 40)

    await cancel_candidate(cfg, cid)

    assert _status(cid) == CandidateStatus.cancelled
    assert [s for _h, s, _e in _rows(cid)] == ["imported", "cancelled", "failed", "cancelled"]
    assert "1" * 40 in fake.torrents  # imported history, and its local copy, are left be
    assert "2" * 40 not in fake.torrents and "4" * 40 not in fake.torrents


async def test_a15_cancel_twice_is_a_no_op(cfg: Any) -> None:
    from homeTheater.acquisition import cancel_candidate

    cid = _seed(status=CandidateStatus.cancelled)

    assert await cancel_candidate(cfg, cid) == "The Matrix"


def _tmdb_details(tmdb_id: int) -> None:
    respx.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": tmdb_id,
                "name": "Silo",
                "first_air_date": "2023-05-05",
                "seasons": [],
            },
        )
    )


@respx.mock
async def test_w9_following_again_reopens_cancelled_seasons_only(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from homeTheater.db import session_scope
    from homeTheater.discovery.actions import follow_series

    cid = _seed(title="Silo", kind=TitleKind.series, season=2, status=CandidateStatus.cancelled)
    rejected = _seed(title="Silo", kind=TitleKind.series, season=3, status=CandidateStatus.rejected)
    imported = _seed(title="Silo", kind=TitleKind.series, season=1, status=CandidateStatus.imported)
    with session_scope() as s:
        s.query(Title).one().tmdb_id = 125988
    _tmdb_details(125988)
    secrets = cfg.secrets.model_copy(update={"tmdb_api_key": SecretStr("k")})

    await follow_series(cfg.model_copy(update={"secrets": secrets}), 125988)

    assert _status(cid) == CandidateStatus.approved
    assert _status(rejected) == CandidateStatus.rejected  # a label, never flipped
    assert _status(imported) == CandidateStatus.imported


def test_w9_set_followed_reopens_too(cfg: Any) -> None:
    from homeTheater.discovery.actions import set_followed

    cid = _seed(title="Silo", kind=TitleKind.series, season=2, status=CandidateStatus.cancelled)

    assert set_followed(603, TitleKind.series, True)

    assert _status(cid) == CandidateStatus.approved


def test_w9_cancelled_can_be_approved_again(cfg: Any) -> None:
    from homeTheater.db import session_scope
    from homeTheater.discovery.actions import approve

    cid = _seed(status=CandidateStatus.cancelled)
    with session_scope() as s:
        s.add(GrabRetry(candidate_id=cid, attempts=2, next_attempt_at=utcnow()))

    assert approve(cid)

    assert _status(cid) == CandidateStatus.approved
    with session_scope() as s:
        assert s.get(GrabRetry, cid) is None


async def test_w9_stopped_candidates_cannot_be_queued_directly(cfg: Any) -> None:
    from homeTheater.errors import InvalidTransitionError

    with pytest.raises(InvalidTransitionError):
        await _queue(cfg, _seed(status=CandidateStatus.cancelled))


# --- A16: a timeout really stops the copy -------------------------------------


def test_a16_copy_stops_at_the_next_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr(importer, "_COPY_CHUNK", 4)
    cancel = threading.Event()
    out = io.BytesIO()

    def stop_after_first(done: int, _total: int) -> None:
        cancel.set()

    with pytest.raises(importer.ImportStoppedError):
        importer._copy_stream(io.BytesIO(b"x" * 40), out, 40, stop_after_first, cancel=cancel)

    assert out.getvalue() == b"x" * 4


def test_a16_stopped_copy_keeps_its_part_to_resume(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_bytes(b"v" * 100)
    lib = tmp_path / "lib"
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(importer.ImportStoppedError):
        importer.LocalLibraryTarget(str(lib)).import_file(
            str(src), "Movies/M", "M.mkv", cancel=cancel
        )

    assert (lib / "Movies" / "M" / "M.mkv.part").exists()
    assert not (lib / "Movies" / "M" / "M.mkv").exists()


async def test_a16_cancelling_the_awaiter_waits_for_the_thread() -> None:
    cancel = threading.Event()
    in_copy = threading.Event()
    finished = threading.Event()

    def copy() -> None:
        in_copy.set()
        cancel.wait(5)
        finished.set()

    task = asyncio.ensure_future(svc._run_copy(cancel, copy))
    while not in_copy.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() or finished.is_set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancel.is_set() and finished.is_set()  # gone before we were told it's over


@respx.mock
async def test_a16_sync_lock_is_held_until_the_copy_thread_ends(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.acquisition import sync_downloads
    from homeTheater.locks import is_job_running

    payload = tmp_path / "dl"
    payload.mkdir()
    (payload / "movie.mkv").write_bytes(b"v" * 10)
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    fake = _client()
    fake.add(percentDone=1.0, status=6, name="movie.mkv", downloadDir=str(payload))

    in_copy = threading.Event()
    seen_cancel: list[bool] = []
    release = threading.Event()

    def slow_import(*_a: Any, cancel: threading.Event, **_kw: Any) -> str:
        in_copy.set()
        seen_cancel.append(cancel.wait(5))
        release.wait(5)
        return "/lib/x"

    monkeypatch.setattr(importer, "import_completed_movie", slow_import)

    task = asyncio.ensure_future(sync_downloads(cfg))
    while not in_copy.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.05)

    assert is_job_running(cfg, "sync")  # the copy thread is still in there
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen_cancel == [True]
    assert not is_job_running(cfg, "sync")
    assert _rows(cid)[0][1] == "completed"  # not left "importing"; the next sweep resumes


# --- A17: a failing import gives up, a flaky NAS doesn't ----------------------


def _failing_import(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def boom(*_a: Any, **_kw: Any) -> str:
        raise exc

    monkeypatch.setattr(importer, "import_completed_movie", boom)


def _complete_torrent(tmp_path: Path) -> Client:
    fake = _client()
    fake.add(percentDone=1.0, status=6, name="movie.mkv", downloadDir=str(tmp_path))
    return fake


def _end_backoff() -> None:
    from homeTheater.db import session_scope

    with session_scope() as s:
        for track in s.query(DownloadTracking):
            track.next_import_at = None


@respx.mock
async def test_a17_import_backs_off_after_a_failure(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "completed", None)])
    _complete_torrent(tmp_path)
    tries: list[int] = []

    def boom(*_a: Any, **_kw: Any) -> str:
        tries.append(1)
        raise importer.ImportError_("nope")

    monkeypatch.setattr(importer, "import_completed_movie", boom)

    await _sync(cfg)
    await _sync(cfg)  # still inside the backoff

    assert len(tries) == 1
    assert _rows(cid)[0][1] == "completed"


@respx.mock
async def test_a17_attempt_cap_parks_for_a_person(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "completed", None)])
    _complete_torrent(tmp_path)
    tries: list[int] = []

    def boom(*_a: Any, **_kw: Any) -> str:
        tries.append(1)
        raise importer.ImportError_("nope")

    monkeypatch.setattr(importer, "import_completed_movie", boom)

    for _ in range(svc._IMPORT_MAX_ATTEMPTS + 2):
        await _sync(cfg)
        _end_backoff()

    assert len(tries) == svc._IMPORT_MAX_ATTEMPTS
    _h, state, error = _rows(cid)[0]
    assert state == "completed"  # still holds its local data as far as anyone can tell
    assert error.startswith(MARKER_NEEDS_ATTENTION) and "nope" in error
    assert _status(cid) == CandidateStatus.downloading


@respx.mock
async def test_a17_nas_faults_are_not_the_items_fault(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-GB file finishing across several EIO sweeps is the design."""
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "completed", None)])
    _complete_torrent(tmp_path)
    tries: list[int] = []

    def boom(*_a: Any, **_kw: Any) -> str:
        tries.append(1)
        raise importer.NasUnavailableError("link faulted")

    monkeypatch.setattr(importer, "import_completed_movie", boom)

    for _ in range(svc._IMPORT_MAX_ATTEMPTS + 2):
        await _sync(cfg)

    assert len(tries) == svc._IMPORT_MAX_ATTEMPTS + 2  # no backoff, no cap
    assert not _rows(cid)[0][2].startswith(MARKER_NEEDS_ATTENTION)


@respx.mock
async def test_a17_library_conflict_parks_at_once(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "completed", None)])
    _complete_torrent(tmp_path)
    _failing_import(monkeypatch, importer.LibraryConflictError("both copies are kept"))

    await _sync(cfg)

    assert _rows(cid)[0][2].startswith(MARKER_NEEDS_ATTENTION)


@respx.mock
async def test_a17_retry_import_puts_it_back_in_line(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.acquisition import retry_import

    error = f"{MARKER_NEEDS_ATTENTION} import failed"
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "completed", error)])
    fake = _complete_torrent(tmp_path)
    monkeypatch.setattr(importer, "import_completed_movie", lambda *_a, **_k: "/lib/x.mkv")
    monkeypatch.setattr(importer, "find_unimported_videos", lambda *_a: [])

    await _sync(cfg)
    assert _rows(cid)[0][1] == "completed"  # parked: the sweep walks past it

    assert retry_import(cid) == 1
    assert _rows(cid)[0] == (HASH, "completed", None)
    await _sync(cfg)

    assert _rows(cid)[0][1] == "imported"
    assert HASH not in fake.torrents


def test_a17_retry_import_unknown_candidate(cfg: Any) -> None:
    from homeTheater.acquisition import retry_import

    with pytest.raises(ValueError, match="not found"):
        retry_import(99)


def test_a17_dropped_mount_counts_as_a_nas_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer.os.path, "ismount", lambda _p: False)

    with pytest.raises(importer.NasUnavailableError):
        importer.ensure_mounted("/Volumes/Share", None)


# --- A19: expanding a series --------------------------------------------------


def _aired(monkeypatch: pytest.MonkeyPatch, *seasons: tuple[int, int]) -> None:
    async def aired(*_a: Any) -> list[Any]:
        return [SimpleNamespace(number=n, episode_count=c) for n, c in seasons]

    monkeypatch.setattr(svc, "_aired_seasons", aired)


@respx.mock
async def test_a19_rejected_first_season_stays_rejected(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    parent = _seed(title="Silo", kind=TitleKind.series)
    rejected = _seed(title="Silo", kind=TitleKind.series, season=1, status=CandidateStatus.rejected)
    _aired(monkeypatch, (1, 3), (2, 3))
    _indexer({"Silo S02": [_row("Silo S02 1080p WEB")]})
    _client()

    outcome = await _queue(cfg, parent)

    assert outcome.queued
    with session_scope() as s:
        assert s.get(Candidate, parent).season == 2  # took the next free season, not S1
        assert s.get(Candidate, rejected).status == CandidateStatus.rejected
    assert _status(rejected) == CandidateStatus.rejected


@respx.mock
async def test_a19_all_owned_series_is_marked_done(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    parent = _seed(title="Silo", kind=TitleKind.series)
    with session_scope() as s:
        title = s.query(Title).one()
        s.add(OwnedFile(path="/a", title_id=title.id, kind=TitleKind.series, season=1, episode=1))
    _aired(monkeypatch, (1, 1))
    _client()

    outcome = await _queue(cfg, parent)

    assert not outcome.queued and "already owned" in outcome.message
    assert _status(parent) == CandidateStatus.imported


@respx.mock
async def test_a19_a_partly_blocked_series_stays_approved(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _seed(title="Silo", kind=TitleKind.series)
    _seed(title="Silo", kind=TitleKind.series, season=1, status=CandidateStatus.queued)
    _aired(monkeypatch, (1, 3))
    _client()

    await _queue(cfg, parent)

    assert _status(parent) == CandidateStatus.approved


@respx.mock
async def test_a22_sibling_features_come_from_the_title(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    parent = _seed(title="Silo", kind=TitleKind.series)
    with session_scope() as s:
        s.get(Candidate, parent).features = {"stale": "parent", "season_episodes": 99}
        s.query(Title).one().imdb_rating = 8.1
    _aired(monkeypatch, (1, 3), (2, None))
    _indexer({})
    _client()

    await _queue(cfg, parent)

    with session_scope() as s:
        sibling = s.query(Candidate).filter(Candidate.id != parent).one()
        assert "stale" not in sibling.features
        assert sibling.features["imdb_rating"] == 8.1
        assert sibling.features["season"] == 2
        assert "season_episodes" not in sibling.features


@respx.mock
async def test_reopened_season_already_owned_doesnt_search_forever(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(title="Silo", kind=TitleKind.series, season=1, target=1)
    with session_scope() as s:
        title = s.query(Title).one()
        s.add(OwnedFile(path="/a", title_id=title.id, kind=TitleKind.series, season=1, episode=1))
    asked = _indexer({})
    _client()

    await _queue(cfg, cid)

    assert asked == []
    assert _status(cid) == CandidateStatus.imported


# --- A20: disk space ----------------------------------------------------------


def _with_download_dir(cfg: Any, path: Path, min_free_gb: float = 5.0) -> Any:
    torrent = cfg.torrent.model_copy(
        update={"movie_download_dir": str(path), "min_free_gb": min_free_gb}
    )
    return cfg.model_copy(update={"torrent": torrent})


@respx.mock
async def test_a20_release_size_counts(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _seed()
    monkeypatch.setattr(svc, "_free_gb", lambda _p: 10.0)
    _indexer({"The Matrix 1999": [_row("The Matrix 1999 1080p WEB", size=8_000_000_000)]})
    fake = _client()

    outcome = await _queue(_with_download_dir(cfg, tmp_path), cid)

    assert not outcome.queued and "low local disk space" in outcome.message
    assert "torrent-add" not in fake.calls
    assert _status(cid) == CandidateStatus.approved


@respx.mock
async def test_a20_bytes_still_downloading_count(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _seed()
    monkeypatch.setattr(svc, "_free_gb", lambda _p: 10.0)
    _indexer({"The Matrix 1999": [_row("The Matrix 1999 1080p WEB", size=1_000_000_000)]})
    fake = _client()
    fake.add(OTHER, leftUntilDone=6_000_000_000)

    outcome = await _queue(_with_download_dir(cfg, tmp_path), cid)

    assert not outcome.queued and "low local disk space" in outcome.message


@respx.mock
async def test_a20_room_enough_grabs(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _seed()
    monkeypatch.setattr(svc, "_free_gb", lambda _p: 10.0)
    _indexer({"The Matrix 1999": [_row("The Matrix 1999 1080p WEB", size=1_000_000_000)]})
    fake = _client()
    fake.add(OTHER, leftUntilDone=1_000_000_000)

    assert (await _queue(_with_download_dir(cfg, tmp_path), cid)).queued


async def test_a20_unreadable_dir_blocks(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _seed()
    monkeypatch.setattr(svc, "_free_gb", lambda _p: None)

    outcome = await _queue(_with_download_dir(cfg, tmp_path), cid)

    assert not outcome.queued and "can't read free space" in outcome.message


def test_a20_dir_not_created_yet_is_measured_at_its_parent(tmp_path: Path) -> None:
    assert svc._free_gb(str(tmp_path / "not" / "yet")) is not None


async def test_a20_check_can_be_disabled(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_free_gb", lambda _p: None)

    assert svc._disk_refusal(_with_download_dir(cfg, tmp_path, 0), TitleKind.movie) is None


@pytest.mark.parametrize(
    ("size", "kept"),
    [(None, True), (0, True), (200 * 1024**2, False), (2 * 1024**3, True), (200 * 1024**3, False)],
)
def test_a20_movie_size_bounds(size: int | None, kept: bool) -> None:
    rel = TorrentRelease("pb", "M 2020 1080p WEB", 9, 1, size, infohash=HASH)

    got = select_release([rel], allowed_resolutions=["1080p"], min_seeders=1, movie=True)

    assert (got is not None) is kept


@pytest.mark.parametrize(
    ("episode", "size", "kept"),
    [
        (1, 20 * 1024**2, False),
        (1, 600 * 1024**2, True),
        (1, 60 * 1024**3, False),
        (None, 60 * 1024**3, True),
        (None, 100 * 1024**2, False),
    ],
)
def test_a20_series_size_bounds(episode: int | None, size: int, kept: bool) -> None:
    name = "Silo S01E01 1080p" if episode else "Silo S01 1080p"
    rel = TorrentRelease("pb", name, 9, 1, size, infohash=HASH)

    got = select_release(
        [rel], allowed_resolutions=["1080p"], min_seeders=1, season=1, episode=episode
    )

    assert (got is not None) is kept


# --- A21 ----------------------------------------------------------------------


def _episode_config() -> Any:
    return SimpleNamespace(nas=SimpleNamespace(tv_root="TV Shows", movies_root="Movies"))


class _Target:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.placed: list[str] = []

    def ready(self) -> None:
        self.order.append("ready")

    def list_dir(self, _rel: str) -> list[str]:
        self.order.append("list")
        return ["Colin.From.Accounts"]

    def import_file(self, _src: str, rel_dir: str, name: str, *_a: Any, **_kw: Any) -> str:
        self.placed.append(rel_dir)
        return f"/lib/{rel_dir}/{name}"


def test_a21_mount_is_checked_before_listing_the_series_folder(tmp_path: Path) -> None:
    video = tmp_path / "Colin.From.Accounts.S01E01.1080p.mkv"
    video.write_bytes(b"v" * 10)
    order: list[str] = []

    importer.import_completed_episodes(
        _episode_config(),
        _Target(order),
        content_path=str(video),
        series_title="Colin From Accounts",
        season=1,
    )

    assert order == ["ready", "list"]


def test_a21_season_zero_is_a_real_season(tmp_path: Path) -> None:
    video = tmp_path / "Show.S00E01.Special.1080p.mkv"
    video.write_bytes(b"v" * 10)
    target = _Target([])

    out = importer.import_completed_episodes(
        _episode_config(), target, content_path=str(video), series_title="Show", season=3
    )

    assert out[0].season == 0
    assert target.placed[0].endswith("Season 00")


def test_a21_ban_holds_for_a_magnet_only_release() -> None:
    magnet = f"magnet:?xt=urn:btih:{HASH.upper()}&dn=x"
    rel = TorrentRelease("1337x", "M 2020 1080p", 900, 1, None, magnet=magnet)

    got = select_release(
        [rel], allowed_resolutions=["1080p"], min_seeders=1, banned=frozenset({HASH})
    )

    assert got is None


def test_a21_btih_reads_base32_magnets() -> None:
    import base64

    encoded = base64.b32encode(bytes.fromhex(HASH)).decode()
    rel = TorrentRelease("x", "M", 1, 1, None, magnet=f"magnet:?xt=urn:btih:{encoded}&dn=x")

    assert rel.btih() == HASH


async def test_a21_rarbg_reads_real_seeders() -> None:
    from homeTheater.acquisition.torrent.sources.rarbg import RarbgSource

    html = (
        "<table><tr class='row'><td>Movies</td>"
        '<td><a href="magnet:?xt=urn:btih:' + HASH + '&amp;dn=Movie.2020.1080p">x</a></td>'
        "<td>2020-01-01</td><td>1.5 GB</td><td><font color='green'>321</font></td>"
        "<td>7</td><td>uploader</td></tr>"
        '<tr><td><a href="magnet:?xt=urn:btih:' + OTHER + '&amp;dn=Other.2020">y</a></td></tr>'
        "</table>"
    )
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, text=html))
    async with httpx.AsyncClient(transport=transport) as http:
        got = await RarbgSource("http://r.test", http, flaresolverr_url=None, timeout=1).search(
            "x", TitleKind.movie
        )

    assert (got[0].seeders, got[0].leechers) == (321, 7)
    assert got[0].size_bytes == int(1.5 * 1024**3)
    assert (got[1].seeders, got[1].size_bytes) == (0, None)  # layout unknown: no guessing


async def test_a21_max_results_per_source_is_honoured(cfg: Any) -> None:
    class Source:
        name = "s"

        async def search(self, _q: str, _k: TitleKind) -> list[TorrentRelease]:
            return [TorrentRelease("s", str(n), n, 0, None) for n in range(1, 50)]

    limited = cfg.model_copy(
        update={"torrent": cfg.torrent.model_copy(update={"max_results_per_source": 3})}
    )

    got = await svc._search_all(limited, [Source()], "q", TitleKind.movie)

    assert [r.seeders for r in got] == [49, 48, 47]  # the best ones


# --- A22: no secrets where they don't belong ----------------------------------


def test_a22_osascript_gets_no_password_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    mounted = {"v": False}

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        seen.update(cmd=cmd, input=kw["input"])
        mounted["v"] = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(importer.os.path, "ismount", lambda _p: mounted["v"])
    monkeypatch.setattr(importer.subprocess, "run", fake_run)

    importer.ensure_mounted("/Volumes/E", importer.SmbMount("nas", "E", "me", "s3cret/with&chars"))

    assert not any("s3cret" in part for part in seen["cmd"])
    assert b"s3cret%2Fwith%26chars" in seen["input"]


async def test_a22_scrapers_redact_what_they_log(monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.acquisition.torrent.sources import rarbg, x1337

    logged: list[str] = []
    for module in (rarbg, x1337):
        monkeypatch.setattr(
            module.log, "warning", lambda _e, **kw: logged.append(str(kw.get("detail")))
        )

    async def blocked(*_a: Any, **_k: Any) -> str:
        raise x1337.ChallengeError("http://x.test/?api_key=SECRET is walled")

    monkeypatch.setattr(rarbg, "fetch_html", blocked)
    monkeypatch.setattr(x1337, "fetch_html", blocked)
    async with httpx.AsyncClient() as http:
        await rarbg.RarbgSource("http://r", http, flaresolverr_url=None, timeout=1).search(
            "q", TitleKind.movie
        )
        source = x1337.X1337Source("http://x", http, flaresolverr_url=None, timeout=1)
        await source.search("q", TitleKind.movie)
        await source._resolve_magnet("/torrent/1/x/")

    assert len(logged) == 3
    assert not any("SECRET" in line for line in logged)


# --- W2 / W16: restart and cancel know their limits ---------------------------


@pytest.mark.parametrize(
    "status", [CandidateStatus.imported, CandidateStatus.rejected, CandidateStatus.cancelled]
)
async def test_w2_restart_refuses_finished_or_stopped(cfg: Any, status: CandidateStatus) -> None:
    from homeTheater.acquisition import restart_candidate
    from homeTheater.errors import InvalidTransitionError

    cid = _seed(status=status, downloads=[(HASH, "imported", None)])

    with pytest.raises(InvalidTransitionError):
        await restart_candidate(cfg, cid)

    assert _status(cid) == status
    assert _rows(cid) == [(HASH, "imported", None)]


@pytest.mark.parametrize(
    "status", [CandidateStatus.imported, CandidateStatus.rejected, CandidateStatus.new]
)
async def test_w2_cancel_refuses_what_isnt_running(cfg: Any, status: CandidateStatus) -> None:
    from homeTheater.acquisition import cancel_candidate
    from homeTheater.errors import InvalidTransitionError

    cid = _seed(status=status)

    with pytest.raises(InvalidTransitionError):
        await cancel_candidate(cfg, cid)

    assert _status(cid) == status


@pytest.mark.parametrize("action", ["restart", "cancel"])
async def test_w16_nothing_acts_mid_import(cfg: Any, action: str) -> None:
    from homeTheater import acquisition
    from homeTheater.errors import InvalidTransitionError

    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "importing", None)])
    act = getattr(acquisition, f"{action}_candidate")

    with pytest.raises(InvalidTransitionError, match="copying"):
        await act(cfg, cid)

    assert _rows(cid) == [(HASH, "importing", None)]
    assert _status(cid) == CandidateStatus.downloading


@respx.mock
async def test_w16_restart_keeps_what_a_season_already_delivered(cfg: Any) -> None:
    from homeTheater.acquisition import restart_candidate

    cid = _seed(
        title="Silo",
        kind=TitleKind.series,
        season=1,
        target=2,
        status=CandidateStatus.failed,
        downloads=[("1" * 40, "imported", None), ("2" * 40, "failed", "x")],
    )
    _indexer({})
    fake = _client()
    fake.add("1" * 40)
    fake.add("2" * 40)

    await restart_candidate(cfg, cid)

    assert _rows(cid) == [("1" * 40, "imported", None)]
    assert "1" * 40 in fake.torrents and "2" * 40 not in fake.torrents


@respx.mock
async def test_w16_sweep_doesnt_import_for_a_cancelled_candidate(cfg: Any, tmp_path: Path) -> None:
    """Listed by the sweep, cancelled before its turn: no copy, torrent removed."""
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "downloading", None)])
    fake = _complete_torrent(tmp_path)
    from homeTheater.db import session_scope

    with session_scope() as s:
        s.get(Candidate, cid).status = CandidateStatus.rejected

    stats = await _sync(cfg)

    assert stats.completed == 0
    assert _rows(cid)[0][1] == "cancelled"
    assert _status(cid) == CandidateStatus.rejected
    assert HASH not in fake.torrents


def test_w16_claim_import_wont_take_a_stopped_row(cfg: Any) -> None:
    cid = _seed(status=CandidateStatus.downloading, downloads=[(HASH, "cancelled", None)])
    from homeTheater.db import session_scope

    with session_scope() as s:
        download_id = s.query(Download).filter_by(candidate_id=cid).one().id

    assert not svc._claim_import(download_id)


# --- W6: a hole isn't a finished season ----------------------------------------


def _finished(cid: int, covered_by: list[Any] | None) -> CandidateStatus:
    from homeTheater.db import session_scope

    with session_scope() as s:
        return svc._status_after_finished(s, s.get(Candidate, cid), 0, covered_by)


def _episode(n: int) -> Any:
    return SimpleNamespace(season=2, episode=n, episode_end=None)


def test_w6_failed_sibling_leaves_the_season_failed(cfg: Any) -> None:
    from homeTheater.db import session_scope

    cid = _seed(
        title="Silo",
        kind=TitleKind.series,
        season=2,
        series_status="Ended",
        downloads=[("1" * 40, "imported", None), ("2" * 40, "failed", "x")],
    )
    with session_scope() as s:
        s.query(Download).filter_by(external_id="2" * 40).one().release = "Silo S02E02 1080p"

    assert _finished(cid, [_episode(1)]) == CandidateStatus.failed


def test_w6_failed_episode_delivered_some_other_way_is_no_hole(cfg: Any) -> None:
    cid = _seed(
        title="Silo",
        kind=TitleKind.series,
        season=2,
        downloads=[("1" * 40, "failed", "x")],
    )

    assert _finished(cid, [_episode(1)]) == CandidateStatus.imported


def test_w6_airing_season_tops_up_rather_than_failing(cfg: Any) -> None:
    cid = _seed(
        title="Silo",
        kind=TitleKind.series,
        season=2,
        target=10,
        downloads=[("2" * 40, "failed", "x")],
    )

    assert _finished(cid, [_episode(5)]) == CandidateStatus.approved


# --- NAS-OVERLAP --------------------------------------------------------------


def test_nas_overlap_advisory_is_visible_while_held(cfg: Any) -> None:
    from homeTheater.locks import import_active, is_import_active

    assert not is_import_active(cfg)
    with import_active(cfg):
        assert is_import_active(cfg)
    assert not is_import_active(cfg)


def test_nas_overlap_advisory_nests(cfg: Any) -> None:
    from homeTheater.locks import import_active, is_import_active

    with import_active(cfg), import_active(cfg):
        assert is_import_active(cfg)


def test_nas_overlap_advisory_failure_never_stops_an_import(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import homeTheater.locks as locks

    def denied(*_a: Any, **_k: Any) -> int:
        raise PermissionError("nope")

    monkeypatch.setattr(locks.os, "open", denied)
    ran = []

    with locks.import_active(cfg):
        ran.append(True)

    assert ran == [True]


def test_nas_overlap_a_look_at_the_wrong_moment_doesnt_stop_an_import(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import homeTheater.locks as locks

    def busy(*_a: Any) -> None:
        raise BlockingIOError

    monkeypatch.setattr(locks.fcntl, "flock", busy)
    ran = []

    with locks.import_active(cfg):
        ran.append(True)

    assert ran == [True]


async def test_nas_overlap_wait_returns_when_the_copy_ends(cfg: Any) -> None:
    from homeTheater.locks import import_active, wait_for_import_gap

    gate = import_active(cfg)
    gate.__enter__()
    waiter = asyncio.ensure_future(wait_for_import_gap(cfg, 5, poll=0.001))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    gate.__exit__(None, None, None)

    assert await waiter is True


async def test_nas_overlap_wait_is_bounded(cfg: Any) -> None:
    from homeTheater.locks import import_active, wait_for_import_gap

    with import_active(cfg):
        assert await wait_for_import_gap(cfg, 0.01, poll=0.001) is False


@respx.mock
async def test_nas_overlap_import_runs_under_the_advisory(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.locks import is_import_active

    _seed(status=CandidateStatus.downloading, downloads=[(HASH, "completed", None)])
    _complete_torrent(tmp_path)
    seen: list[bool] = []

    def spy(*_a: Any, **_k: Any) -> str:
        seen.append(is_import_active(cfg))
        return "/lib/x.mkv"

    monkeypatch.setattr(importer, "import_completed_movie", spy)
    monkeypatch.setattr(importer, "find_unimported_videos", lambda *_a: [])

    await _sync(cfg)

    assert seen == [True]
    assert not is_import_active(cfg)


async def test_nas_overlap_subtitle_sweep_waits_between_items(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.subtitles.native import service as native

    order: list[str] = []

    async def gap(*_a: Any, **_k: Any) -> bool:
        order.append("gap")
        return True

    async def prepare(_c: Any, item: Any) -> Any:
        order.append("item")
        return SimpleNamespace(present=False)

    async def ready(_c: Any) -> bool:
        return True

    monkeypatch.setattr(native, "wait_for_import_gap", gap)
    monkeypatch.setattr(native, "_prepare", prepare)
    monkeypatch.setattr(native, "_library_ready", ready)
    item = SimpleNamespace(kind=TitleKind.movie, lang="en", season=None, episode=None)
    source = SimpleNamespace(supports=lambda _l: True, name="s")

    await native._process_queue(cfg, [source], [item, item], native.NativeSweepStats())

    assert order == ["gap", "item", "gap", "item"]


async def test_nas_overlap_scheduled_scan_defers(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import homeTheater.locks as locks
    from homeTheater.scheduler import jobs

    scans: list[int] = []
    monkeypatch.setattr("homeTheater.scanner.run_scan", lambda _c: scans.append(1))
    monkeypatch.setattr(locks, "SCAN_IMPORT_WAIT_S", 0.01)

    with locks.import_active(cfg):
        await jobs.run_scan_job(cfg)

    assert scans == []
    assert "scan deferred" in jobs.skip_reasons["scan"]


async def test_nas_overlap_scan_runs_when_nothing_is_copying(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import jobs

    scans: list[int] = []

    def scan(_c: Any) -> Any:
        scans.append(1)
        return SimpleNamespace(files_added=0)

    monkeypatch.setattr("homeTheater.scanner.run_scan", scan)

    await jobs.run_scan_job(cfg)

    assert scans == [1]
