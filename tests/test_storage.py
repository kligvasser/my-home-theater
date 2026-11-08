"""F9: the storage planner. It reads; nothing here deletes."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from homeTheater import storage
from homeTheater.acquisition.torrent.base import TorrentStatus
from homeTheater.db import init_db, session_scope
from homeTheater.db.base import utcnow
from homeTheater.db.models import (
    Candidate,
    CandidateSource,
    CandidateStatus,
    Download,
    OwnedFile,
    Setting,
    Title,
    TitleKind,
)

GB = 10**9


def _cfg(config_file: Path, tmp_path: Path, **torrent: Any) -> Any:
    from homeTheater.config import get_config

    get_config.cache_clear()
    init_db()
    cfg = get_config().model_copy(deep=True)
    cfg.acquisition.backend = "torrent"
    cfg.torrent.library_base_dir = torrent.get("base", str(tmp_path))
    return cfg


def _title(name: str, *files: tuple[str, int | None], **fields: Any) -> int:
    kind = fields.pop("kind", TitleKind.movie)
    res = fields.pop("resolution", None)
    with session_scope() as s:
        title = Title(title=name, kind=kind, **fields)
        title.owned_files = [
            OwnedFile(path=p, kind=kind, size_bytes=size, resolution=res) for p, size in files
        ]
        s.add(title)
        s.flush()
        return title.id


def _fixed_usage(monkeypatch: pytest.MonkeyPatch, total: int = 100, free: int = 40) -> None:
    monkeypatch.setattr(
        storage, "_disk_usage", lambda path: (total * GB, (total - free) * GB, free * GB)
    )


async def test_capacity_is_cached(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    calls: list[str] = []

    def usage(path: str) -> tuple[int, int, int]:
        calls.append(path)
        return 100, 60, 40

    monkeypatch.setattr(storage, "_disk_usage", usage)
    first = await storage.capacity(cfg)
    second = await storage.capacity(cfg)
    assert (first.total, first.used, first.free) == (100, 60, 40)
    assert second is first and len(calls) == 1


async def test_cache_expires_on_the_wall_clock(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop time stops while the Mac sleeps; a reading from before bed isn't fresh."""

    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(storage.time, "time", lambda: now[0])
    first = await storage.capacity(cfg)
    now[0] += storage.CAPACITY_TTL_S + 1
    assert await storage.capacity(cfg) is not first


async def test_a_hung_mount_times_out_and_never_stacks_workers(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    monkeypatch.setattr(storage, "CAPACITY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(storage, "CAPACITY_RETRY_S", 0.0)
    release = threading.Event()
    calls: list[str] = []

    def hung(path: str) -> tuple[int, int, int]:
        calls.append(path)
        release.wait(5)
        return 100, 60, 40

    monkeypatch.setattr(storage, "_disk_usage", hung)
    try:
        first = await storage.capacity(cfg)
        second = await storage.capacity(cfg)
        assert first.free is None and "didn't answer" in first.note
        assert second.free is None and "hasn't come back" in second.note
        assert len(calls) == 1
    finally:
        release.set()
    for _ in range(100):
        if not storage._busy:
            break
        await asyncio.sleep(0.01)
    assert (await storage.capacity(cfg)).free == 40  # free to try again


async def test_a_failed_read_falls_back_to_the_last_good_one(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    monkeypatch.setattr(storage, "CAPACITY_TTL_S", 0.0)
    await storage.capacity(cfg)

    def broken(path: str) -> tuple[int, int, int]:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(storage, "_disk_usage", broken)
    reading = await storage.capacity(cfg)
    assert reading.free == 40 * GB and reading.stale is True and "Input/output" in reading.note


async def test_an_unmounted_share_is_never_measured(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty /Volumes folder would report the boot disk's free space as the NAS's."""

    cfg = _cfg(config_file, tmp_path, base="/Volumes/Share")

    async def mounts() -> list[str]:
        return ["/", "/Volumes/Other"]

    def must_not(path: str) -> tuple[int, int, int]:
        raise AssertionError("stat on an unmounted path")

    monkeypatch.setattr(storage, "_mount_points", mounts)
    monkeypatch.setattr(storage, "_disk_usage", must_not)
    reading = await storage.capacity(cfg)
    assert reading.free is None and "isn't mounted" in reading.note


async def test_a_mounted_share_is_measured(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path, base="/Volumes/Share/Media")

    async def mounts() -> list[str]:
        return ["/", "/Volumes/Share"]

    monkeypatch.setattr(storage, "_mount_points", mounts)
    _fixed_usage(monkeypatch)
    assert (await storage.capacity(cfg)).free == 40 * GB


async def test_without_a_mount_it_says_so(config_file: Path) -> None:
    from homeTheater.config import get_config

    reading = await storage.capacity(get_config())
    assert reading.free is None and "No local NAS mount" in reading.note


async def test_usage_per_root_and_title(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _title("Big", ("/m/big.mkv", 10 * GB))
    _title("Small", ("/m/small.mkv", GB), ("/m/unknown.mkv", None))
    _title("Show", ("/t/a.mkv", 2 * GB), ("/t/b.mkv", 2 * GB), kind=TitleKind.series)

    plan = await storage.storage_plan(cfg)
    roots = {r.kind: r for r in plan.roots}
    assert roots["movie"].root == "Movies" and roots["movie"].bytes == 11 * GB
    assert roots["movie"].unsized == 1 and roots["series"].files == 2
    assert [t.title for t in plan.titles] == ["Big", "Show", "Small"]
    assert plan.titles[1].files == 2 and plan.titles[1].bytes == 4 * GB


def _history(*entries: tuple[int, int]) -> None:
    today = date.today()
    rows = [
        {"day": (today - timedelta(days=ago)).isoformat(), "used": used, "total": 100}
        for ago, used in entries
    ]
    with session_scope() as s:
        s.add(Setting(key=storage.HISTORY_KEY, value=json.dumps(rows)))


async def test_forecast_from_measured_history_counts_grabs_under_way(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch, free=40)
    _history((10, 40 * GB), (0, 60 * GB))  # 2 GB a day
    title = _title("Typical", ("/m/typical.mkv", 4 * GB))
    _grab(title)

    plan = await storage.storage_plan(cfg)
    assert plan.growth.source == "nas" and plan.growth.bytes_per_day == 2 * GB
    assert plan.in_flight.bytes == 4 * GB and plan.in_flight.estimated == 1
    assert plan.days_to_full == 18  # (40 - 4) / 2


async def test_shrinking_usage_has_no_forecast(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _history((10, 60 * GB), (0, 40 * GB))
    plan = await storage.storage_plan(cfg)
    assert plan.growth.bytes_per_day is not None and plan.growth.bytes_per_day < 0
    assert plan.days_to_full is None


async def test_catalog_growth_stands_in_and_ignores_the_first_scan(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch, free=100)
    now = utcnow()
    title = _title("Library", ("/m/a.mkv", 3000 * GB))  # all of it, on day one
    with session_scope() as s:
        boot = s.scalars(select(OwnedFile)).one()
        boot.created_at = now - timedelta(days=30)
        s.add(
            OwnedFile(
                title_id=title,
                path="/m/b.mkv",
                kind=TitleKind.movie,
                size_bytes=29 * GB,
                created_at=now - timedelta(days=5),
            )
        )
    plan = await storage.storage_plan(cfg)
    assert plan.growth.source == "catalog" and plan.growth.days == 29
    assert plan.growth.bytes_per_day == GB


async def test_too_little_history_gives_no_rate_rather_than_a_wild_one(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _title("Just scanned", ("/m/a.mkv", 3000 * GB))
    plan = await storage.storage_plan(cfg)
    assert plan.growth.bytes_per_day is None and plan.days_to_full is None


async def test_snapshot_is_once_a_day_and_skips_a_dead_mount(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    assert await storage.record_snapshot(cfg) is True
    assert await storage.record_snapshot(cfg) is True
    assert len(storage._load_history()) == 1

    from homeTheater.config import get_config

    assert await storage.record_snapshot(get_config()) is False


async def test_storage_job_skips_quietly_when_the_nas_cannot_be_read(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    init_db()
    sent: list[str] = []

    async def fake_notify(config: Any, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)
    await jobs.run_storage_job(get_config())
    assert sent == [] and "storage" in jobs.skip_reasons


def _grab(title_id: int, *, infohash: str | None = None, state: str = "downloading") -> None:
    with session_scope() as s:
        cand = Candidate(
            title_id=title_id, source=CandidateSource.manual, status=CandidateStatus.downloading
        )
        s.add(cand)
        s.flush()
        s.add(Download(candidate_id=cand.id, external_id=infohash, state=state))


async def test_in_flight_is_the_whole_torrent_not_what_is_left(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _grab(_title("Grab"), infohash="abc")

    class Poller:
        async def poll(self, hashes: frozenset[str]) -> tuple[dict[str, TorrentStatus], bool]:
            status = TorrentStatus(
                "abc", 0.75, True, False, None, left_bytes=GB
            )  # a quarter left of 4 GB
            return {"abc": status}, False

    monkeypatch.setattr("homeTheater.pipeline._live_poller", lambda config: Poller())
    flight = await storage._in_flight(cfg)
    assert flight.bytes == 4 * GB and flight.estimated == 0


async def test_in_flight_survives_transmission_being_down(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _title("Typical", ("/m/a.mkv", 2 * GB))
    _grab(_title("Grab"), infohash="abc")

    def down(config: Any) -> Any:
        raise RuntimeError("no client")

    monkeypatch.setattr("homeTheater.pipeline._live_poller", down)
    flight = await storage._in_flight(cfg)
    assert (flight.downloads, flight.bytes, flight.estimated) == (1, 2 * GB, 1)


async def _suggestions(cfg: Any) -> Any:
    return (await storage.storage_plan(cfg)).suggestions


def _watch(*paths: str) -> None:
    now = utcnow()
    with session_scope() as s:
        for path in paths:
            owned = s.scalars(select(OwnedFile).where(OwnedFile.path == path)).one()
            s.execute(
                text(
                    "INSERT INTO watch_state (owned_file_id, play_count, source, updated_at) "
                    "VALUES (:id, 1, 'kodi', :now)"
                ),
                {"id": owned.id, "now": now},
            )


async def test_ranking_watched_then_low_rating_then_duplicates(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _title("Copies", ("/m/copy.720p.mkv", 9 * GB), ("/m/copy.1080p.mkv", 20 * GB))
    _title("Bad", ("/m/bad.mkv", 5 * GB), imdb_rating=4.2)
    _title("Seen", ("/m/seen.mkv", GB), imdb_rating=9.0)
    _title("Keeper", ("/m/keep.mkv", 50 * GB), imdb_rating=8.0)
    _watch("/m/seen.mkv")
    with session_scope() as s:
        for f in s.scalars(select(OwnedFile).where(OwnedFile.path.like("/m/copy%"))):
            f.resolution = "720p" if "720p" in f.path else "1080p"

    plan = await storage.storage_plan(cfg)
    assert [x.title for x in plan.suggestions] == ["Seen", "Bad", "Copies"]
    assert plan.suggestions[1].reasons == ("rated 4.2",)
    copies = plan.suggestions[2]
    assert copies.files == ("copy.720p.mkv",) and copies.bytes == 9 * GB
    assert plan.reclaimable == 15 * GB and plan.watch_data is True


async def test_no_watch_table_is_no_opinion(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _title("Bad", ("/m/bad.mkv", GB), imdb_rating=4.0)
    with session_scope() as s:
        s.execute(text("DROP TABLE watch_state"))
    plan = await storage.storage_plan(cfg)
    assert plan.watch_data is False and [x.title for x in plan.suggestions] == ["Bad"]


async def test_a_series_is_watched_only_when_every_episode_is(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    title = _title("Show", ("/t/e1.mkv", GB), ("/t/e2.mkv", GB), kind=TitleKind.series)
    with session_scope() as s:
        for n, f in enumerate(s.scalars(select(OwnedFile).where(OwnedFile.title_id == title))):
            f.season, f.episode = 1, n + 1
    _watch("/t/e1.mkv")
    assert await _suggestions(cfg) == []
    _watch("/t/e2.mkv")
    assert (await _suggestions(cfg))[0].reasons == ("watched",)


async def test_followed_and_in_flight_titles_are_never_suggested(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _title("Followed", ("/m/a.mkv", GB), imdb_rating=2.0, followed=True)
    busy = _title("Busy", ("/m/b.mkv", GB), imdb_rating=2.0)
    _grab(busy)
    assert await _suggestions(cfg) == []


async def test_missing_ratings_are_not_low(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _title("Unrated", ("/m/a.mkv", GB))
    _title("TMDb low", ("/m/b.mkv", GB), tmdb_rating=3.0)
    assert [x.title for x in await _suggestions(cfg)] == ["TMDb low"]


async def test_parts_and_episodes_are_not_duplicates(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _title("Long Film", ("/m/film.cd1.avi", GB), ("/m/film.cd2.avi", GB))
    show = _title("Show", ("/t/e1.mkv", GB), ("/t/e2.mkv", GB), kind=TitleKind.series)
    with session_scope() as s:
        for n, f in enumerate(s.scalars(select(OwnedFile).where(OwnedFile.title_id == show))):
            f.season, f.episode = 1, n + 1
    assert await _suggestions(cfg) == []


async def test_keeps_the_copy_with_subtitles_at_equal_quality(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(config_file, tmp_path)
    _fixed_usage(monkeypatch)
    _title("Film", ("/m/subbed.mkv", GB), ("/m/plain.mkv", 2 * GB), resolution="1080p")
    with session_scope() as s:
        s.scalars(
            select(OwnedFile).where(OwnedFile.path == "/m/subbed.mkv")
        ).one().subtitle_langs = ["he"]
    assert (await _suggestions(cfg))[0].files == ("plain.mkv",)


@pytest.fixture
def client(config_file: Path) -> Iterator[TestClient]:
    from homeTheater.api.app import create_app

    init_db()
    with TestClient(create_app()) as c:
        yield c


def test_page_renders_empty_and_full(client: TestClient) -> None:
    empty = client.get("/storage")
    assert empty.status_code == 200 and "Nothing cataloged yet" in empty.text
    assert "No local NAS mount" in empty.text
    _title("<b>Bad</b>", ("/m/bad.mkv", GB), imdb_rating=3.0)
    page = client.get("/storage").text
    assert "&lt;b&gt;Bad&lt;/b&gt;" in page and "rated 3.0" in page
    assert "nothing on this page deletes" in page


def test_the_planner_exposes_no_way_to_delete(client: TestClient) -> None:
    assert client.post("/storage").status_code == 405
    assert client.delete("/storage").status_code == 405
