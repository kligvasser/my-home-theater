"""Season-scoped torrent grabs: pack verification, per-episode fallback for
airing seasons, episode top-up, and the multi-download candidate lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.acquisition.torrent.base import TorrentRelease
from homeTheater.acquisition.torrent.select import select_release
from homeTheater.db.models import TitleKind

APIBAY = "http://apibay.test"
TRANSMISSION = "http://transmission.test/transmission/rpc"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool = False) -> None:
    cfg = tmp_path / "torrent.yaml"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'torrent.db'}'}}\n"
        f"features: {{dry_run: {str(dry_run).lower()}, auto_approve: false}}\n"
        "acquisition: {backend: torrent}\n"
        "torrent:\n"
        "  enabled_sources: [piratebay]\n"
        "  min_seeders: 1\n"
        f"  piratebay_api_url: {APIBAY}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    monkeypatch.setenv("TRANSMISSION_URL", TRANSMISSION)


def _seed_season_candidate(season: int = 3, season_episodes: int | None = 3) -> int:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title

    init_db()
    feats: dict = {"season": season}
    if season_episodes:
        feats["season_episodes"] = season_episodes
    with session_scope() as s:
        t = Title(tmdb_id=125988, title="Silo", year=2023, kind=TitleKind.series)
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id,
            season=season,
            source=CandidateSource.discovery,
            status=CandidateStatus.approved,
            features=feats,
        )
        s.add(c)
        s.flush()
        return c.id


def _row(name: str, infohash: str, seeders: int = 100) -> dict:
    return {
        "id": "1",
        "name": name,
        "info_hash": infohash,
        "seeders": str(seeders),
        "leechers": "1",
        "size": "1000000",
    }


def _apibay_by_query(catalog: dict[str, list[dict]]) -> None:
    """Mock apibay: answer each query with its catalog rows (else no results)."""

    def respond(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        return httpx.Response(200, json=catalog.get(q, []))

    respx.get(f"{APIBAY}/q.php").mock(side_effect=respond)


def _transmission_accepts_adds() -> None:
    """Mock Transmission: echo back the btih of whatever magnet gets added."""

    session_negotiated = {"done": False}

    def respond(request: httpx.Request) -> httpx.Response:
        if not session_negotiated["done"]:
            session_negotiated["done"] = True
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "sid"})
        body = json.loads(request.content)
        magnet = body["arguments"]["filename"]
        btih = magnet.split("btih:")[1].split("&")[0]
        return httpx.Response(
            200,
            json={
                "result": "success",
                "arguments": {"torrent-added": {"hashString": btih, "name": "x"}},
            },
        )

    respx.post(TRANSMISSION).mock(side_effect=respond)


# --- selection (pure) -------------------------------------------------------


def test_select_verifies_season_and_rejects_other_packs() -> None:
    releases = [
        TorrentRelease(
            "pb", "Silo (2023) Season 1 S01 (1080p WEB x265)", 900, 1, 1, infohash="a" * 40
        ),
        TorrentRelease("pb", "Silo S01-S03 Complete 1080p", 500, 1, 1, infohash="b" * 40),
        TorrentRelease("pb", "Silo S03E01 1080p WEB h264", 400, 1, 1, infohash="c" * 40),
        TorrentRelease("pb", "Silo S03 COMPLETE 1080p WEB H264", 50, 1, 1, infohash="d" * 40),
    ]
    picked = select_release(releases, allowed_resolutions=["1080p"], min_seeders=1, season=3)
    assert picked is not None and picked.infohash == "d" * 40  # the actual S3 pack

    # No qualifying S4 release at all — never fall back to a wrong season.
    assert select_release(releases, allowed_resolutions=["1080p"], min_seeders=1, season=4) is None


def test_select_targets_specific_episode_including_multi_episode_files() -> None:
    releases = [
        TorrentRelease("pb", "Silo S03E01 1080p WEB h264", 400, 1, 1, infohash="a" * 40),
        TorrentRelease("pb", "Silo S03E01E02 1080p WEB", 300, 1, 1, infohash="b" * 40),
    ]
    picked = select_release(
        releases, allowed_resolutions=["1080p"], min_seeders=1, season=3, episode=2
    )
    assert picked is not None and picked.infohash == "b" * 40  # E02 lives in the double


# --- queue: pack vs airing-season episodes ----------------------------------


@respx.mock
async def test_queue_grabs_verified_season_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    _reset()
    cid = _seed_season_candidate()
    _apibay_by_query(
        {
            # The "Silo S03" query returns a decoy S1 pack + the real S3 pack.
            "Silo S03": [
                _row("Silo (2023) Season 1 S01 (1080p WEB x265)", "a" * 40, seeders=900),
                _row("Silo S03 COMPLETE 1080p WEB H264", "d" * 40, seeders=50),
            ],
        }
    )
    _transmission_accepts_adds()

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    outcome = await queue_candidate(get_config(), cid)

    assert outcome.queued and "season pack" in outcome.message
    with session_scope() as s:
        dl = s.query(Download).one()
        assert dl.external_id == "d" * 40  # S3 verified; S1 decoy rejected
        assert s.get(Candidate, cid).status == CandidateStatus.queued


@respx.mock
async def test_queue_airing_season_grabs_available_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    _reset()
    cid = _seed_season_candidate(season_episodes=3)
    _apibay_by_query(
        {
            # No S3 pack anywhere; only old-season packs + two aired episodes.
            "Silo S03": [_row("Silo Season 2 COMPLETE 1080p WEB", "a" * 40)],
            "Silo Season 3": [_row("Silo Season 1 COMPLETE 1080p WEB", "b" * 40)],
            "Silo S03E01": [_row("Silo S03E01 1080p WEB h264", "c" * 40)],
            "Silo S03E02": [_row("Silo S03E02 1080p WEB h264", "e" * 40)],
            # E03 not aired yet -> no rows
        }
    )
    _transmission_accepts_adds()

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    outcome = await queue_candidate(get_config(), cid)

    assert outcome.queued
    assert "2 episode releases" in outcome.message and "top up" in outcome.message
    with session_scope() as s:
        hashes = {d.external_id for d in s.query(Download).all()}
        assert hashes == {"c" * 40, "e" * 40}
        assert s.get(Candidate, cid).status == CandidateStatus.queued


@respx.mock
async def test_queue_topup_skips_already_grabbed_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approved season candidate with imported episodes re-queues only the
    missing ones (the weekly top-up), and never re-tries the pack."""

    _write_config(tmp_path, monkeypatch)
    _reset()
    cid = _seed_season_candidate(season_episodes=3)

    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    with session_scope() as s:
        s.add(
            Download(
                candidate_id=cid,
                external_id="c" * 40,
                state="imported",
                release="Silo S03E01 1080p WEB h264",
            )
        )

    _apibay_by_query(
        {
            "Silo S03E02": [_row("Silo S03E02 1080p WEB h264", "e" * 40)],
            # E03 still unaired
        }
    )
    _transmission_accepts_adds()

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config

    outcome = await queue_candidate(get_config(), cid)

    assert outcome.queued and "E02" in outcome.message and "E01" not in outcome.message
    with session_scope() as s:
        hashes = {d.external_id for d in s.query(Download).all()}
        assert hashes == {"c" * 40, "e" * 40}
        assert s.get(Candidate, cid).status == CandidateStatus.queued


@respx.mock
async def test_queue_no_new_episodes_reports_without_grabbing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    _reset()
    cid = _seed_season_candidate(season_episodes=3)

    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    with session_scope() as s:
        s.add(
            Download(
                candidate_id=cid,
                external_id="c" * 40,
                state="imported",
                release="Silo S03E01 1080p WEB h264",
            )
        )

    _apibay_by_query({})  # nothing new out this week

    from homeTheater.acquisition import queue_candidate
    from homeTheater.config import get_config

    outcome = await queue_candidate(get_config(), cid)

    assert not outcome.queued and "no suitable release" in outcome.message
    with session_scope() as s:
        assert s.query(Download).count() == 1  # unchanged
        assert s.get(Candidate, cid).status == CandidateStatus.approved


# --- sync: multi-download lifecycle ------------------------------------------


def _torrent_status(infohash: str, done: bool) -> dict:
    return {
        "hashString": infohash,
        "name": "x",
        "percentDone": 1.0 if done else 0.5,
        "status": 6 if done else 4,
        "downloadDir": "/dl",
        "error": 0,
        "errorString": "",
    }


def _transmission_statuses(by_hash: dict[str, bool]) -> None:
    session_negotiated = {"done": False}

    def respond(request: httpx.Request) -> httpx.Response:
        if not session_negotiated["done"]:
            session_negotiated["done"] = True
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "sid"})
        body = json.loads(request.content)
        wanted = body["arguments"]["ids"][0]
        torrents = [_torrent_status(h, d) for h, d in by_hash.items() if h == wanted]
        return httpx.Response(200, json={"result": "success", "arguments": {"torrents": torrents}})

    respx.post(TRANSMISSION).mock(side_effect=respond)


def _seed_episode_download(cid: int, episode: int, infohash: str, state: str) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Download

    with session_scope() as s:
        s.add(
            Download(
                candidate_id=cid,
                external_id=infohash,
                state=state,
                release=f"Silo S03E{episode:02d} 1080p WEB h264",
            )
        )


@respx.mock
async def test_sync_partial_season_returns_to_approved_for_topup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last in-flight episode finishes but the season isn't complete: candidate
    goes back to approved so the next acquire run looks for the rest."""

    _write_config(tmp_path, monkeypatch)
    _reset()
    cid = _seed_season_candidate(season_episodes=3)
    _seed_episode_download(cid, 1, "c" * 40, "downloading")
    _transmission_statuses({"c" * 40: True})

    from homeTheater.acquisition import sync_downloads
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    stats = await sync_downloads(get_config())

    assert stats.completed == 1
    with session_scope() as s:
        assert s.query(Download).one().state == "imported"
        assert s.get(Candidate, cid).status == CandidateStatus.approved  # 1 of 3


@respx.mock
async def test_sync_full_season_flips_imported_only_when_all_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    _reset()
    cid = _seed_season_candidate(season_episodes=2)
    _seed_episode_download(cid, 1, "c" * 40, "downloading")
    _seed_episode_download(cid, 2, "e" * 40, "downloading")
    _transmission_statuses({"c" * 40: True, "e" * 40: True})

    from homeTheater.acquisition import sync_downloads
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus

    stats = await sync_downloads(get_config())

    assert stats.completed == 2
    with session_scope() as s:
        assert s.get(Candidate, cid).status == CandidateStatus.imported  # 2 of 2


@respx.mock
async def test_sync_one_stalled_episode_does_not_fail_active_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    _write_config(tmp_path, monkeypatch)
    _reset()
    cid = _seed_season_candidate(season_episodes=2)
    _seed_episode_download(cid, 1, "c" * 40, "downloading")
    _seed_episode_download(cid, 2, "e" * 40, "downloading")

    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Download

    with session_scope() as s:
        stale = s.query(Download).filter(Download.external_id == "c" * 40).one()
        stale.created_at = datetime.now(UTC) - timedelta(hours=10)  # past the 6h grace

    # E01 gone from the client entirely; E02 still transferring at 50%.
    _transmission_statuses({"e" * 40: False})

    from homeTheater.acquisition import sync_downloads
    from homeTheater.config import get_config

    stats = await sync_downloads(get_config())

    assert stats.failed == 1
    with session_scope() as s:
        states = {d.external_id: d.state for d in s.query(Download).all()}
        assert states["c" * 40] == "failed" and states["e" * 40] == "downloading"
        # the candidate keeps riding the live download instead of failing
        assert s.get(Candidate, cid).status == CandidateStatus.downloading
