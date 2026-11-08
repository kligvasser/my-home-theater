"""Cleanup: NAS-extras discovery/deletion and stuck-torrent classification."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

TRANSMISSION = "http://transmission.test/transmission/rpc"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lib = tmp_path / "lib"
    cfg = tmp_path / "cleanup.yaml"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'c.db'}'}}\n"
        "acquisition: {backend: torrent}\n"
        "torrent:\n"
        "  enabled_sources: [piratebay]\n"
        f"  library_base_dir: {lib}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    monkeypatch.setenv("TRANSMISSION_URL", TRANSMISSION)
    _reset()
    return lib


def test_find_and_delete_nas_extras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    season = lib / "TV Shows" / "Ted Lasso" / "Season 01"
    (season / "Featurettes").mkdir(parents=True)
    (season / "Behind.The.Scenes").mkdir()
    ep = season / "Ted Lasso (2020) - S01E01 - Pilot.mkv"
    ep.write_bytes(b"e" * 100)
    extra1 = season / "Featurettes" / "The Cast Ask Each Other Anything.mkv"
    extra1.write_bytes(b"x" * 50)
    extra2 = season / "Behind.The.Scenes" / "Set Tour.mkv"
    extra2.write_bytes(b"y" * 30)
    extra3 = season / "ted.lasso.season.1.trailer.mkv"
    extra3.write_bytes(b"z" * 20)
    extra4 = season / "ted-lasso-sample.mkv"
    extra4.write_bytes(b"s" * 10)

    from homeTheater.cleanup import delete_nas_extras, find_nas_extras
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    init_db()
    extras, notes = find_nas_extras(get_config())
    paths = {e.path for e in extras}
    assert paths == {str(extra1), str(extra2), str(extra3), str(extra4)}  # the episode is kept
    assert notes == []

    deleted, errors = delete_nas_extras(get_config(), list(paths))
    assert deleted == 4 and errors == []
    assert ep.exists() and not extra1.exists() and not extra2.exists()


@pytest.mark.parametrize(
    "relative",
    [
        # No episode number is not evidence: all real episodes (L4).
        "Fauda/עונה 1/עונה 1 פרק 3.mkv",
        "Fauda/Season 1/פרק 7.mkv",
        "Mini Series/Part.2.mkv",
        "Daily Show/The.Daily.Show.2024.03.15.mkv",
        "Lost/Season 01/Pilot.mkv",
        "Ted Lasso/Season 01/Season 1 - The Cast Ask Each Other Anything - IMDB.mkv",
        # Evidence that doesn't hold up.
        "Fauda/Season 1/Featurettes/עונה 1 פרק 3.mkv",  # Hebrew episode name wins
        "Show/Season 01/Show.S02E03.The.Sample.mkv",  # an episode number wins
        "Extras/Season 1/Extras - Pilot.mkv",  # the show is called Extras
        "Trailer Park Boys/Trailer Park Boys - Pilot.mkv",  # so is this one's title
        "Sampler/Season 1/Sampler Special.mkv",  # a word, not a substring
    ],
)
def test_nas_extras_need_positive_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    path = lib / "TV Shows" / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"e" * 100)

    from homeTheater.cleanup import find_nas_extras
    from homeTheater.config import get_config

    assert find_nas_extras(get_config())[0] == []


def test_nas_extras_size_cap_overrides_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    folder = lib / "TV Shows" / "Show" / "Extras"
    folder.mkdir(parents=True)

    from homeTheater.cleanup import EXTRA_MAX_BYTES, find_nas_extras
    from homeTheater.config import get_config

    with open(folder / "at the cap.mkv", "wb") as f:
        f.truncate(EXTRA_MAX_BYTES)
    with open(folder / "over the cap.mkv", "wb") as f:
        f.truncate(EXTRA_MAX_BYTES + 1)

    assert [e.path for e in find_nas_extras(get_config())[0]] == [str(folder / "at the cap.mkv")]


def test_find_nas_extras_notes_when_no_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'c.db'}'}}\n"
        "acquisition: {backend: torrent}\n"
        "torrent: {enabled_sources: [piratebay]}\n"  # no library_base_dir
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    _reset()

    from homeTheater.cleanup import find_nas_extras
    from homeTheater.config import get_config

    extras, notes = find_nas_extras(get_config())
    assert extras == [] and notes and "not set" in notes[0]


def _transmission_list(torrents: list[dict]) -> None:
    session = {"done": False}

    def respond(request: httpx.Request) -> httpx.Response:
        if not session["done"]:
            session["done"] = True
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "s"})
        return httpx.Response(200, json={"result": "success", "arguments": {"torrents": torrents}})

    respx.post(TRANSMISSION).mock(side_effect=respond)


def _t(h: str, name: str, done: bool) -> dict:
    return {
        "hashString": h,
        "name": name,
        "percentDone": 1.0 if done else 0.4,
        "status": 6 if done else 4,
        "downloadDir": "/d",
        "error": 0,
        "errorString": "",
    }


def _seed_downloads(rows: list[tuple[str, str, str | None]]) -> None:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import (
        Candidate,
        CandidateSource,
        CandidateStatus,
        Download,
        Title,
        TitleKind,
    )

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=1, title="X", kind=TitleKind.movie)
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id, source=CandidateSource.discovery, status=CandidateStatus.imported
        )
        s.add(c)
        s.flush()
        seen: set[str] = set()
        for infohash, state, save_path in rows:
            owner = c
            if infohash in seen:  # one row per (candidate, hash): a second candidate shares it
                owner = Candidate(title_id=t.id, source=CandidateSource.discovery, status=c.status)
                s.add(owner)
                s.flush()
            seen.add(infohash)
            s.add(
                Download(
                    candidate_id=owner.id,
                    external_id=infohash,
                    state=state,
                    release="R",
                    save_path=save_path,
                )
            )


@respx.mock
async def test_find_stuck_torrents_classifies_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an imported download with its NAS copy verified is removable; every
    other completed torrent is unmanaged; pending and incomplete are left alone."""
    lib = _write_config(tmp_path, monkeypatch)
    movie = lib / "Movies" / "X (2020)" / "X.mkv"
    movie.parent.mkdir(parents=True)
    movie.write_bytes(b"m" * 10)
    season = lib / "TV Shows" / "S" / "Season 01"
    season.mkdir(parents=True)
    (season / "S.S01E01.mkv").write_bytes(b"e" * 10)
    empty_season = lib / "TV Shows" / "S" / "Season 02"
    empty_season.mkdir()
    empty_file = lib / "Movies" / "Y.mkv"
    empty_file.write_bytes(b"")

    local = tmp_path / "downloads" / "X.mkv"
    local.parent.mkdir()
    local.write_bytes(b"m" * 10)

    hashes = {name: f"{i:x}" * 40 for i, name in enumerate("abcdefghijklm", start=1)}
    h = hashes
    _seed_downloads(
        [
            (h["a"], "imported", str(movie)),
            (h["b"], "imported", str(season)),
            (h["c"], "completed", "/d"),
            (h["e"], "imported", str(local)),  # exists, but it never reached the library
            (h["f"], "imported", str(lib / "Movies" / "Gone.mkv")),
            (h["g"], "imported", None),
            (h["h"], "imported", str(empty_season)),
            (h["i"], "failed", str(movie)),
            (h["j"], "cancelled", None),
            (h["k"], "imported", str(empty_file)),
            # One pack, two rows: the failed season's only copy is in this torrent.
            (h["l"], "imported", str(movie)),
            (h["l"], "failed", None),
        ]
    )
    _transmission_list(
        [
            *[_t(h[n], n, True) for n in "abcdefghijkl"],
            _t(h["m"], "m", False),
        ]
    )

    from homeTheater.cleanup import find_stuck_torrents
    from homeTheater.config import get_config

    stuck, unmanaged, notes = await find_stuck_torrents(get_config())

    assert {t.infohash: t.reason for t in stuck} == {
        h["a"]: "already imported",
        h["b"]: "already imported",
    }
    by_name = {t.name: t.reason for t in unmanaged}
    assert set(by_name) == set("defghijkl")  # not c (pending), not m (incomplete)
    assert by_name["d"] == "no download record"
    assert by_name["e"] == by_name["f"] == by_name["g"] == by_name["h"] == by_name["k"]
    assert "not verified" in by_name["e"]
    assert by_name["i"] == "download failed"
    assert by_name["j"] == "download cancelled"
    assert by_name["l"] == "download failed"
    assert notes == []


@respx.mock
async def test_stuck_torrents_unverified_when_mount_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    movie = lib / "X.mkv"
    movie.parent.mkdir(parents=True)
    movie.write_bytes(b"m")
    _seed_downloads([("a" * 40, "imported", str(movie))])
    _transmission_list([_t("a" * 40, "Movie", True)])

    def dropped(base: str, mount: object) -> None:
        raise RuntimeError("NAS share not mounted")

    monkeypatch.setattr("homeTheater.acquisition.torrent.importer.ensure_mounted", dropped)

    from homeTheater.cleanup import find_stuck_torrents
    from homeTheater.config import get_config

    stuck, unmanaged, _ = await find_stuck_torrents(get_config())
    assert stuck == [] and [t.name for t in unmanaged] == ["Movie"]


@respx.mock
async def test_find_stuck_torrents_notes_when_client_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    from homeTheater.db import init_db

    init_db()
    respx.post(TRANSMISSION).mock(side_effect=httpx.ConnectError("refused"))

    from homeTheater.cleanup import find_stuck_torrents
    from homeTheater.config import get_config

    stuck, unmanaged, notes = await find_stuck_torrents(get_config())
    assert stuck == [] and unmanaged == [] and notes and "unreachable" in notes[0]


async def test_cleanup_yields_to_running_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cleanup and sync share the NAS; plan/apply must refuse while the sync lock
    is held (an import in progress) rather than thrash the mount concurrently."""
    _write_config(tmp_path, monkeypatch)
    from homeTheater.cleanup import apply_cleanup, plan_cleanup
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.errors import JobBusyError
    from homeTheater.locks import job_lock

    init_db()
    cfg = get_config()
    with job_lock(cfg, "sync"):  # simulate an import holding the lock
        with pytest.raises(JobBusyError):
            await plan_cleanup(cfg)
        with pytest.raises(JobBusyError):
            await apply_cleanup(cfg, extra_paths=[], infohashes=[])


def _no_torrents(monkeypatch: pytest.MonkeyPatch, stuck: list[Any] | None = None) -> None:
    async def find(config: object) -> tuple[list[Any], list[Any], list[str]]:
        return stuck or [], [], []

    monkeypatch.setattr("homeTheater.cleanup.find_stuck_torrents", find)


def _extras(lib: Path, count: int) -> list[Path]:
    folder = lib / "TV Shows" / "Show" / "Season 01" / "Extras"
    folder.mkdir(parents=True)
    # Letters, not digits: guessit reads "Extra 12" as an episode number.
    paths = [folder / f"Making of {'x' * (i + 1)}.mkv" for i in range(count)]
    for path in paths:
        path.write_bytes(b"x")
    return paths


async def test_apply_acts_only_on_reviewed_items_still_in_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    reviewed, unreviewed = _extras(lib, 2)
    episode = lib / "TV Shows" / "Show" / "Season 01" / "Show.S01E01.mkv"
    episode.write_bytes(b"e")
    vanished_hash, new_hash, ok_hash = "a" * 40, "b" * 40, "c" * 40

    from homeTheater.cleanup import StuckTorrent, apply_cleanup
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    init_db()
    _no_torrents(
        monkeypatch,
        [StuckTorrent(ok_hash, "ok", "already imported"), StuckTorrent(new_hash, "new", "x")],
    )
    removed: list[list[str]] = []

    async def fake_remove(config: object, hashes: list[str]) -> None:
        removed.append(hashes)

    monkeypatch.setattr("homeTheater.acquisition.torrent.service.remove_torrents", fake_remove)

    result = await apply_cleanup(
        get_config(),
        extra_paths=[str(reviewed), str(episode)],  # the episode isn't in the plan
        infohashes=[ok_hash.upper(), vanished_hash],
    )

    assert not reviewed.exists()
    assert unreviewed.exists()  # found since the review: waits for its own
    assert episode.exists()
    assert removed == [[ok_hash]]
    assert result["extras_deleted"] == 1 and result["torrents_removed"] == 1
    assert any("2 reviewed item(s)" in n for n in result["notes"])


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, TestClient]:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    lib = _write_config(tmp_path, monkeypatch)
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    return lib, TestClient(create_app(), headers={"X-Auth-Token": "tok"})


def test_api_preview_lists_every_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib, client = _client(tmp_path, monkeypatch)
    paths = _extras(lib, 60)
    _no_torrents(monkeypatch)

    with client:
        body = client.get("/api/cleanup").json()

    assert body["extras_count"] == 60
    assert {e["path"] for e in body["extras"]} == {str(p) for p in paths}
    assert body["unmanaged"] == [] and body["unmanaged_count"] == 0


def test_api_apply_requires_the_reviewed_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib, client = _client(tmp_path, monkeypatch)
    paths = _extras(lib, 3)
    _no_torrents(monkeypatch)

    with client:
        assert client.post("/api/cleanup").status_code == 422
        assert client.post("/api/cleanup", json={"extras": []}).status_code == 422
        assert all(p.exists() for p in paths)

        r = client.post("/api/cleanup", json={"extras": [str(paths[1])], "hashes": []})

    assert r.status_code == 200 and r.json()["extras_deleted"] == 1
    assert [p.exists() for p in paths] == [True, False, True]


def test_numbered_extra_is_kept_as_a_possible_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    folder = lib / "TV Shows" / "Show" / "Extras"
    folder.mkdir(parents=True)
    (folder / "Extra 12.mkv").write_bytes(b"x")

    from homeTheater.cleanup import find_nas_extras
    from homeTheater.config import get_config

    assert find_nas_extras(get_config())[0] == []


def test_cli_apply_passes_the_plan_it_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, monkeypatch)
    from homeTheater import cleanup as module
    from homeTheater import main
    from homeTheater.cleanup import CleanupPlan, ExtraFile, StuckTorrent

    plan = CleanupPlan(
        extras=[ExtraFile(f"/lib/Extra {i}.mkv", 1) for i in range(45)],
        stuck=[StuckTorrent("a" * 40, "Stuck", "already imported")],
        unmanaged=[StuckTorrent("b" * 40, "Mine", "no download record")],
    )
    seen: dict[str, Any] = {}

    async def fake_plan(config: object) -> CleanupPlan:
        return plan

    async def fake_apply(config: object, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(module, "plan_cleanup", fake_plan)
    monkeypatch.setattr(module, "apply_cleanup", fake_apply)

    main.cleanup(apply=True)

    assert seen["extra_paths"] == [e.path for e in plan.extras]
    assert seen["infohashes"] == ["a" * 40]
    out = capsys.readouterr().out
    assert "Extra 44.mkv" in out  # past the old 40-line cut
    assert "[no download record] Mine" in out


def _drain_nas_pool() -> None:
    from homeTheater import cleanup

    cleanup._nas_pool.submit(lambda: None).result(timeout=5)


async def _ticks_while(coro: Any) -> tuple[Any, int]:
    """Await ``coro`` and count how often the event loop got to run meanwhile."""

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        return await coro, ticks
    finally:
        task.cancel()


async def test_wedged_mount_cannot_freeze_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    from homeTheater import cleanup
    from homeTheater.config import get_config

    release = threading.Event()

    def wedged(config: object) -> tuple[list[Any], list[str]]:
        release.wait(5)
        return [], []

    monkeypatch.setattr(cleanup, "find_nas_extras", wedged)
    monkeypatch.setattr(cleanup, "_FIND_TIMEOUT_SECONDS", 0.05)
    _no_torrents(monkeypatch)

    try:
        plan, ticks = await _ticks_while(cleanup._plan(get_config()))
    finally:
        release.set()
        _drain_nas_pool()

    assert ticks > 0
    assert plan.extras == [] and "gave up" in plan.notes[0]


async def test_timed_out_delete_stops_between_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    first, second = _extras(lib, 2)
    from homeTheater import cleanup
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    init_db()
    _no_torrents(monkeypatch)
    monkeypatch.setattr(cleanup, "_DELETE_TIMEOUT_SECONDS", 0.05)
    release = threading.Event()
    real_rename = os.rename

    def wedged_rename(path: str, target: str) -> None:
        if path == str(first):
            release.wait(5)
        real_rename(path, target)

    monkeypatch.setattr(cleanup.os, "rename", wedged_rename)

    try:
        result, ticks = await _ticks_while(
            cleanup.apply_cleanup(
                get_config(), extra_paths=[str(first), str(second)], infohashes=[]
            )
        )
    finally:
        release.set()
        _drain_nas_pool()

    assert ticks > 0
    assert any("gave up" in e for e in result["errors"])
    assert second.exists()  # the abandoned thread didn't carry on


async def test_timed_out_verification_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    movie = lib / "X.mkv"
    movie.parent.mkdir(parents=True)
    movie.write_bytes(b"m")
    _seed_downloads([("a" * 40, "imported", str(movie))])

    from homeTheater import cleanup
    from homeTheater.config import get_config

    release = threading.Event()

    def wedged(config: object, paths: list[str]) -> set[str]:
        release.wait(5)
        return set(paths)

    monkeypatch.setattr(cleanup, "_verified_paths", wedged)
    monkeypatch.setattr(cleanup, "_VERIFY_TIMEOUT_SECONDS", 0.05)

    with respx.mock:
        _transmission_list([_t("a" * 40, "Movie", True)])
        try:
            stuck, unmanaged, notes = await cleanup.find_stuck_torrents(get_config())
        finally:
            release.set()
            _drain_nas_pool()

    assert stuck == [] and [t.name for t in unmanaged] == ["Movie"]
    assert "not verified" in notes[0]
