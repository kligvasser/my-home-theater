"""Native torrent backend: selection, apibay parsing, Transmission RPC, and the
dry-run/real/sync flows — all external calls mocked via respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.acquisition.torrent.base import TorrentRelease
from homeTheater.acquisition.torrent.select import select_release
from homeTheater.acquisition.torrent.transmission import TransmissionClient
from homeTheater.db.models import TitleKind

APIBAY = "http://apibay.test"
TRANSMISSION = "http://transmission.test/transmission/rpc"
HASH = "a" * 40  # a valid-looking 40-hex btih


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _write_config(
    tmp_path: Path,
    *,
    dry_run: bool,
    monkeypatch: pytest.MonkeyPatch,
    library_base_dir: str | None = None,
    delete_local: bool = False,
) -> None:
    cfg = tmp_path / "torrent.yaml"
    torrent = (
        "torrent:\n"
        "  enabled_sources: [piratebay]\n"
        "  min_seeders: 1\n"
        f"  piratebay_api_url: {APIBAY}\n"
        f"  delete_local_after_import: {str(delete_local).lower()}\n"
    )
    if library_base_dir is not None:
        torrent += f"  library_base_dir: {library_base_dir}\n"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'torrent.db'}'}}\n"
        f"features: {{dry_run: {str(dry_run).lower()}, auto_approve: false}}\n"
        "acquisition: {backend: torrent}\n" + torrent
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    monkeypatch.setenv("TRANSMISSION_URL", TRANSMISSION)


def _seed_approved() -> int:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=603, title="The Matrix", year=1999, kind=TitleKind.movie)
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id, source=CandidateSource.discovery, status=CandidateStatus.approved
        )
        s.add(c)
        s.flush()
        return c.id


def _apibay_rows() -> list[dict]:
    return [
        {  # sentinel "no results" row — must be filtered out
            "id": "0",
            "name": "No results returned",
            "info_hash": "0" * 40,
            "seeders": "0",
            "leechers": "0",
            "size": "0",
        },
        {
            "id": "1",
            "name": "The Matrix 1999 720p BluRay x264",
            "info_hash": "b" * 40,
            "seeders": "200",
            "leechers": "3",
            "size": "900000000",
        },
        {
            "id": "2",
            "name": "The Matrix 1999 1080p BluRay x264",
            "info_hash": HASH,
            "seeders": "120",
            "leechers": "5",
            "size": "1500000000",
        },
    ]


# --- selection (pure) -------------------------------------------------------


def test_select_prefers_allowed_resolution_over_seeders() -> None:
    releases = [
        TorrentRelease("piratebay", "Movie 720p", 500, 1, 1, infohash="a" * 40),
        TorrentRelease("piratebay", "Movie 1080p", 100, 1, 1, infohash="b" * 40),
    ]
    picked = select_release(releases, allowed_resolutions=["1080p", "2160p"], min_seeders=1)
    assert picked is not None and picked.title == "Movie 1080p"  # 720p dropped despite seeders


def test_select_drops_under_seeded_and_magnetless() -> None:
    releases = [
        TorrentRelease("piratebay", "Movie 1080p", 0, 0, 1, infohash="c" * 40),  # too few seeders
        TorrentRelease("piratebay", "Movie 1080p", 50, 1, 1),  # no infohash/magnet
    ]
    assert select_release(releases, allowed_resolutions=["1080p"], min_seeders=5) is None


def test_select_ranks_unknown_resolution_last_then_by_seeders() -> None:
    releases = [
        TorrentRelease("piratebay", "Movie DVDRip", 999, 1, 1, infohash="d" * 40),
        TorrentRelease("piratebay", "Movie 1080p", 10, 1, 1, infohash="e" * 40),
    ]
    picked = select_release(releases, allowed_resolutions=["1080p", "2160p"], min_seeders=1)
    assert picked is not None and picked.title == "Movie 1080p"


def test_magnet_uri_built_from_infohash() -> None:
    rel = TorrentRelease("piratebay", "Some Movie 1080p", 10, 1, 1, infohash=HASH)
    magnet = rel.magnet_uri()
    assert magnet is not None
    assert magnet.startswith(f"magnet:?xt=urn:btih:{HASH}")
    assert "tr=" in magnet  # trackers appended


# --- apibay source ----------------------------------------------------------


@respx.mock
async def test_piratebay_source_parses_and_filters() -> None:
    from homeTheater.acquisition.torrent.sources import PirateBaySource

    respx.get(f"{APIBAY}/q.php").mock(return_value=httpx.Response(200, json=_apibay_rows()))
    async with httpx.AsyncClient() as http:
        src = PirateBaySource(APIBAY, http, timeout=5.0)
        results = await src.search("The Matrix 1999", TitleKind.movie)

    assert len(results) == 2  # sentinel row dropped
    assert {r.infohash for r in results} == {"b" * 40, HASH}
    top = next(r for r in results if r.infohash == HASH)
    assert top.seeders == 120 and top.size_bytes == 1_500_000_000


# --- Transmission client ----------------------------------------------------


@respx.mock
async def test_transmission_add_negotiates_session_id() -> None:
    add_ok = {
        "result": "success",
        "arguments": {"torrent-added": {"hashString": HASH.upper(), "name": "The Matrix"}},
    }
    route = respx.post(TRANSMISSION).mock(
        side_effect=[
            httpx.Response(409, headers={"X-Transmission-Session-Id": "sid-123"}),
            httpx.Response(200, json=add_ok),
        ]
    )
    async with httpx.AsyncClient() as http:
        client = TransmissionClient(TRANSMISSION, http)
        added = await client.add_magnet("magnet:?xt=urn:btih:" + HASH, download_dir=None)

    assert added.infohash == HASH  # lower-cased
    assert route.call_count == 2  # 409 handshake then the real add
    assert route.calls[1].request.headers["X-Transmission-Session-Id"] == "sid-123"


@respx.mock
async def test_transmission_status_reports_completion() -> None:
    torrents = {
        "result": "success",
        "arguments": {
            "torrents": [
                {
                    "hashString": HASH,
                    "name": "x",
                    "percentDone": 1.0,
                    "status": 6,
                    "downloadDir": "/downloads",
                    "error": 0,
                    "errorString": "",
                }
            ]
        },
    }
    respx.post(TRANSMISSION).mock(
        side_effect=[
            httpx.Response(409, headers={"X-Transmission-Session-Id": "s"}),
            httpx.Response(200, json=torrents),
        ]
    )
    async with httpx.AsyncClient() as http:
        st = await TransmissionClient(TRANSMISSION, http).status(HASH)

    assert st is not None and st.complete and not st.downloading
    assert st.save_path == "/downloads"


# --- end-to-end queue / sync ------------------------------------------------


@respx.mock
async def test_queue_dry_run_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, dry_run=True, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()
    respx.get(f"{APIBAY}/q.php").mock(return_value=httpx.Response(200, json=_apibay_rows()))

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    outcome = await queue_candidate(get_config(), cid)

    assert outcome.dry_run and not outcome.queued
    assert "would grab" in outcome.message
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.approved  # unchanged
        assert s.query(Download).count() == 0  # nothing grabbed


@respx.mock
async def test_queue_real_grabs_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    _reset()
    cid = _seed_approved()

    respx.get(f"{APIBAY}/q.php").mock(return_value=httpx.Response(200, json=_apibay_rows()))
    add_ok = {
        "result": "success",
        "arguments": {"torrent-added": {"hashString": HASH, "name": "The Matrix 1999 1080p"}},
    }
    respx.post(TRANSMISSION).mock(
        side_effect=[
            httpx.Response(409, headers={"X-Transmission-Session-Id": "sid"}),
            httpx.Response(200, json=add_ok),
        ]
    )

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    outcome = await queue_candidate(get_config(), cid)

    assert outcome.queued
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.queued
        dl = s.query(Download).one()
        assert dl.external_id == HASH  # the 1080p pick, not the higher-seeded 720p
        assert dl.release == "The Matrix 1999 1080p BluRay x264"


def _complete_status(download_dir: str, name: str) -> dict:
    return {
        "result": "success",
        "arguments": {
            "torrents": [
                {
                    "hashString": HASH,
                    "name": name,
                    "percentDone": 1.0,
                    "status": 6,
                    "downloadDir": download_dir,
                    "error": 0,
                    "errorString": "",
                }
            ]
        },
    }


def _seed_downloading(cid: int) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    with session_scope() as s:
        s.add(Download(candidate_id=cid, external_id=HASH, state="downloading", release="m"))
        s.get(Candidate, cid).status = CandidateStatus.downloading


@respx.mock
async def test_sync_imports_completed_movie_into_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    dl_dir = tmp_path / "dl"
    dl_dir.mkdir()
    (dl_dir / "movie.mkv").write_bytes(b"video-bytes" * 100)
    _write_config(tmp_path, dry_run=False, monkeypatch=monkeypatch, library_base_dir=str(lib))
    _reset()
    cid = _seed_approved()  # Title: The Matrix (1999)
    _seed_downloading(cid)

    respx.post(TRANSMISSION).mock(
        side_effect=[
            httpx.Response(409, headers={"X-Transmission-Session-Id": "s"}),
            httpx.Response(200, json=_complete_status(str(dl_dir), "movie.mkv")),
        ]
    )

    from homeTheater.acquisition import sync_downloads
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    stats = await sync_downloads(get_config())

    assert stats.completed == 1
    dest = lib / "Movies" / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dest.exists() and dest.read_bytes() == b"video-bytes" * 100
    assert not dest.with_suffix(".mkv.part").exists()  # atomic: no leftover .part
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.imported
        row = s.query(Download).one()
        assert row.state == "imported" and row.save_path == str(dest)


@respx.mock
async def test_sync_import_failure_leaves_completed_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path, dry_run=False, monkeypatch=monkeypatch, library_base_dir=str(tmp_path / "lib")
    )
    _reset()
    cid = _seed_approved()
    _seed_downloading(cid)

    # Points at a file that doesn't exist -> import raises, must not mark imported.
    respx.post(TRANSMISSION).mock(
        side_effect=[
            httpx.Response(409, headers={"X-Transmission-Session-Id": "s"}),
            httpx.Response(200, json=_complete_status(str(tmp_path / "dl"), "missing.mkv")),
        ]
    )

    from homeTheater.acquisition import sync_downloads
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    stats = await sync_downloads(get_config())

    assert stats.completed == 0 and stats.errors
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.downloading  # not imported
        row = s.query(Download).one()
        assert row.state == "completed" and row.error and "import failed" in row.error


@respx.mock
async def test_sync_delete_local_after_import_removes_torrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dl_dir = tmp_path / "dl"
    dl_dir.mkdir()
    (dl_dir / "movie.mkv").write_bytes(b"x" * 50)
    _write_config(
        tmp_path,
        dry_run=False,
        monkeypatch=monkeypatch,
        library_base_dir=str(tmp_path / "lib"),
        delete_local=True,
    )
    _reset()
    cid = _seed_approved()
    _seed_downloading(cid)

    route = respx.post(TRANSMISSION).mock(
        side_effect=[
            httpx.Response(409, headers={"X-Transmission-Session-Id": "s"}),
            httpx.Response(200, json=_complete_status(str(dl_dir), "movie.mkv")),
            httpx.Response(200, json={"result": "success", "arguments": {}}),  # torrent-remove
        ]
    )

    from homeTheater.acquisition import sync_downloads
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus

    await sync_downloads(get_config())

    remove_calls = [c for c in route.calls if b'"torrent-remove"' in c.request.content]
    assert remove_calls and b'"delete-local-data":true' in remove_calls[0].request.content
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.imported


# --- importer (unit) --------------------------------------------------------


def test_find_primary_video_picks_largest_skips_sample(tmp_path: Path) -> None:
    from homeTheater.acquisition.torrent.importer import find_primary_video

    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "big.mkv").write_bytes(b"x" * 5000)
    (folder / "sample.mkv").write_bytes(b"y" * 100)  # tiny + "sample" -> skipped
    (folder / "readme.txt").write_bytes(b"z" * 99999)  # not media -> ignored
    assert find_primary_video(str(folder)) == str(folder / "big.mkv")

    single = tmp_path / "one.mp4"
    single.write_bytes(b"a" * 10)
    assert find_primary_video(str(single)) == str(single)
    assert find_primary_video(str(tmp_path / "nope.mkv")) is None


def test_import_completed_movie_local_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path, dry_run=False, monkeypatch=monkeypatch, library_base_dir=str(tmp_path / "lib")
    )
    _reset()
    src = tmp_path / "dl" / "m.mkv"
    src.parent.mkdir()
    src.write_bytes(b"movie" * 100)

    from homeTheater.acquisition.torrent.importer import (
        build_library_target,
        import_completed_movie,
    )
    from homeTheater.config import get_config

    cfg = get_config()
    dest = import_completed_movie(
        cfg, build_library_target(cfg), content_path=str(src), title="The Matrix", year=1999
    )

    assert dest.endswith("Movies/The Matrix (1999)/The Matrix (1999).mkv")
    assert Path(dest).read_bytes() == b"movie" * 100
