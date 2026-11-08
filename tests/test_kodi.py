"""F7: Kodi JSON-RPC, all HTTP mocked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from homeTheater import kodi
from homeTheater.db.models import OwnedFile, Setting, Title, TitleKind, WatchState
from homeTheater.errors import NotConfiguredError

URL = "http://kodi.test:8080/jsonrpc"
MAP = {"smb://NAS/Share/": "\\\\NAS\\Share\\"}
UNC = "\\\\NAS\\Share\\Movies\\Film (2020)\\Film.mkv"
SMB = "smb://NAS/Share/Movies/Film (2020)/Film.mkv"


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    debounce: int = 900,
    path_map: bool = True,
    timezone: str = "UTC",
) -> Any:
    from homeTheater.config import get_config, loader
    from homeTheater.db import init_db
    from homeTheater.db import session as db_session

    mapping = json.dumps(MAP if path_map else {})
    (tmp_path / "k.yaml").write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'k.db'}'}}\n"
        "kodi:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        f"  url: {URL}\n"
        f"  timezone: {timezone}\n"
        f"  scan_debounce_seconds: {debounce}\n"
        f"  path_map: {mapping}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(tmp_path / "k.yaml"))
    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    init_db()
    return get_config()


def _own(path: str = UNC) -> int:
    from homeTheater.db import session_scope

    with session_scope() as s:
        title = Title(title=path, kind=TitleKind.movie)
        s.add(title)
        s.flush()
        owned = OwnedFile(title_id=title.id, path=path, kind=TitleKind.movie)
        s.add(owned)
        s.flush()
        return owned.id


def _reply(movies: list[dict[str, Any]], episodes: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        key = "movies" if method.endswith("GetMovies") else "episodes"
        items = movies if key == "movies" else episodes
        limits = body["params"]["limits"]
        page = items[limits["start"] : limits["end"]]
        return httpx.Response(
            200,
            json={
                "id": 1,
                "jsonrpc": "2.0",
                "result": {"limits": {"total": len(items)}, key: page},
            },
        )

    return handler


def _watch(owned_id: int) -> WatchState | None:
    from homeTheater.db import session_scope

    with session_scope() as s:
        row = s.get(WatchState, owned_id)
        if row is not None:
            s.expunge(row)
        return row


def _movie(file: str = SMB, plays: int = 2, last: str = "2026-03-01 21:00:00") -> dict[str, Any]:
    return {"movieid": 1, "label": "x", "file": file, "playcount": plays, "lastplayed": last}


def test_longest_prefix_wins_and_slashes_become_unc() -> None:
    both = {**MAP, "smb://NAS/Share/TV/": "\\\\Other\\TV"}
    assert kodi.map_kodi_path("smb://NAS/Share/TV/a/b.mkv", both) == "\\\\Other\\TV\\a\\b.mkv"
    assert kodi.map_kodi_path(SMB, MAP) == UNC


def test_a_prefix_only_matches_at_a_folder_boundary() -> None:
    assert kodi.map_kodi_path("smb://NAS/Share2/x.mkv", {"smb://NAS/Share": "\\\\N\\S"}) is None


def test_mapping_to_a_local_path_keeps_forward_slashes() -> None:
    assert kodi.map_kodi_path("smb://N/S/a/b.mkv", {"smb://N/S": "/v/"}) == "/v/a/b.mkv"


def test_logins_are_stripped_from_kodi_paths() -> None:
    assert kodi.strip_userinfo("smb://me:hunter2@NAS/Share/a.mkv") == "smb://NAS/Share/a.mkv"
    stacked = kodi.strip_userinfo("stack://smb://me:pw@N/a.mkv , smb://me:pw@N/b.mkv")
    assert "pw" not in stacked


def test_lastplayed_is_read_in_kodis_zone() -> None:
    got = kodi.parse_kodi_time("2026-07-01 22:00:00", "Asia/Jerusalem")
    assert got == datetime(2026, 7, 1, 19, 0, tzinfo=UTC)
    assert kodi.parse_kodi_time("", "UTC") is None
    assert kodi.parse_kodi_time("garbage", "UTC") is None


@respx.mock
async def test_history_lands_on_the_owned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _own()
    respx.post(URL).mock(side_effect=_reply([_movie()], []))

    stats = await kodi.sync_history(config)

    assert stats.mapped == 1 and stats.updated == 1 and stats.unmapped == 0
    row = _watch(owned)
    assert row is not None and row.play_count == 2 and row.source == "kodi"
    assert row.last_played_at is not None
    assert row.last_played_at.replace(tzinfo=UTC) == datetime(2026, 3, 1, 21, 0, tzinfo=UTC)


@respx.mock
async def test_asks_for_exactly_the_fields_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    route = respx.post(URL).mock(side_effect=_reply([], []))

    await kodi.sync_history(config)

    calls = [json.loads(c.request.content) for c in route.calls]
    assert [c["method"] for c in calls] == ["VideoLibrary.GetMovies", "VideoLibrary.GetEpisodes"]
    assert set(calls[0]["params"]["properties"]) == {"playcount", "lastplayed", "file"}


@respx.mock
async def test_large_libraries_are_paged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(kodi, "PAGE_SIZE", 2)
    route = respx.post(URL).mock(
        side_effect=_reply([_movie(f"smb://NAS/Share/{i}.mkv") for i in range(5)], [])
    )

    stats = await kodi.sync_history(config)

    assert stats.seen == 5
    assert route.call_count == 4  # three of movies, one of episodes


@respx.mock
async def test_unmapped_paths_are_reported_and_never_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.watch import KODI_STATUS_KEY, load_status

    config = _setup(tmp_path, monkeypatch)
    owned = _own()
    respx.post(URL).mock(
        side_effect=_reply(
            [
                _movie("smb://me:hunter2@Other/Share/Film.mkv"),
                _movie("smb://NAS/Share/Movies/Unknown.mkv"),
                _movie("stack://smb://NAS/Share/a.mkv , smb://NAS/Share/b.mkv"),
            ],
            [],
        )
    )

    stats = await kodi.sync_history(config)

    assert stats.unmapped == 3 and stats.updated == 0
    assert _watch(owned) is None
    status = load_status(KODI_STATUS_KEY)
    assert status is not None and status["unmapped"] == 3
    assert "hunter2" not in json.dumps(status)
    assert {u["reason"] for u in status["unmapped_paths"]} == {
        "no kodi.path_map prefix covers it",
        "not in the catalog after mapping",
    }


@respx.mock
async def test_percent_encoded_paths_still_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _own()
    respx.post(URL).mock(side_effect=_reply([_movie(SMB.replace(" ", "%20"))], []))

    await kodi.sync_history(config)

    assert _watch(owned) is not None


@respx.mock
async def test_unwatched_files_make_no_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _own()
    respx.post(URL).mock(side_effect=_reply([_movie(plays=0, last="")], []))

    await kodi.sync_history(config)

    assert _watch(owned) is None


@respx.mock
async def test_login_is_sent_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KODI_USER", "kodi")
    monkeypatch.setenv("KODI_PASS", "pw")
    config = _setup(tmp_path, monkeypatch)
    route = respx.post(URL).mock(side_effect=_reply([], []))

    await kodi.sync_history(config)

    assert route.calls[0].request.headers["authorization"].startswith("Basic ")


@respx.mock
async def test_a_refused_login_says_which_secrets_to_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    respx.post(URL).mock(return_value=httpx.Response(401))

    with pytest.raises(NotConfiguredError, match="KODI_USER"):
        await kodi.sync_history(config)


@respx.mock
async def test_json_rpc_error_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"error": {"code": -32601, "message": "no"}})
    )

    with pytest.raises(kodi.KodiUnavailableError, match="-32601"):
        await kodi.sync_history(config)


@respx.mock
async def test_kodi_off_is_a_quiet_skip_for_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import JobRun
    from homeTheater.scheduler import jobs

    config = _setup(tmp_path, monkeypatch, debounce=0)
    await kodi.note_import(config)
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused http://u:hunter2@kodi"))
    sent: list[str] = []

    async def notify(_config: Any, message: str) -> None:
        sent.append(message)

    monkeypatch.setattr(jobs, "notify", notify)

    await jobs.run_kodi_job(config)
    await jobs.run_kodi_scan_job(config)

    assert jobs.skip_reasons["kodi"] == "Kodi is not reachable"
    assert jobs.skip_reasons["kodi_scan"] == "Kodi is not reachable"
    assert "hunter2" not in json.dumps(jobs.skip_reasons)
    assert sent == []
    with session_scope() as s:
        assert s.query(JobRun).count() == 0


def _age_request(seconds: int) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow

    with session_scope() as s:
        row = s.get(Setting, kodi.SCAN_WANTED_KEY)
        assert row is not None
        row.value = json.dumps((utcnow() - timedelta(seconds=seconds)).isoformat())


@respx.mock
async def test_a_seasons_worth_of_imports_is_one_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"result": "OK"}))

    for _ in range(10):
        await kodi.note_import(config)
    assert await kodi.scan_if_due(config) is False  # still inside the quiet period
    _age_request(1000)
    assert await kodi.scan_if_due(config) is True
    assert await kodi.scan_if_due(config) is False  # answered: nothing left to do

    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["method"] == "VideoLibrary.Scan"


@respx.mock
async def test_each_new_import_restarts_the_quiet_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"result": "OK"}))

    await kodi.note_import(config)
    _age_request(1000)
    await kodi.note_import(config)

    assert await kodi.scan_if_due(config) is False
    assert route.call_count == 0


@respx.mock
async def test_a_scan_waits_out_an_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.locks import import_active

    config = _setup(tmp_path, monkeypatch, debounce=0)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"result": "OK"}))
    await kodi.note_import(config)

    with import_active(config):
        assert await kodi.scan_if_due(config) is False
    assert route.call_count == 0
    assert await kodi.scan_if_due(config) is True


@respx.mock
async def test_a_request_survives_kodi_being_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch, debounce=0)
    await kodi.note_import(config)
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(kodi.KodiUnavailableError):
        await kodi.scan_if_due(config)

    respx.post(URL).mock(return_value=httpx.Response(200, json={"result": "OK"}))
    assert await kodi.scan_if_due(config) is True


@respx.mock
async def test_an_import_during_the_call_keeps_its_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    config = _setup(tmp_path, monkeypatch, debounce=0)
    await kodi.note_import(config)

    async def scan_while_importing(_self: Any) -> None:
        _age_request(5)  # a newer value lands while Kodi is answering

    monkeypatch.setattr(kodi.KodiClient, "scan", scan_while_importing)
    assert await kodi.scan_if_due(config) is True

    with session_scope() as s:
        assert s.get(Setting, kodi.SCAN_WANTED_KEY) is not None


async def test_note_import_is_a_no_op_when_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    config = _setup(tmp_path, monkeypatch, enabled=False)

    await kodi.note_import(config)

    with session_scope() as s:
        assert s.get(Setting, kodi.SCAN_WANTED_KEY) is None


async def test_note_import_swallows_a_database_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)

    def boom() -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(kodi, "_mark_scan_wanted", boom)
    await kodi.note_import(config)  # no exception


def test_jobs_register_only_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.scheduler.catchup import job_intervals
    from homeTheater.scheduler.scheduler import _JOBS

    off = _setup(tmp_path, monkeypatch, enabled=False)
    assert not {"kodi", "kodi_scan"} & set(job_intervals(off.schedule, off))
    on = _setup(tmp_path, monkeypatch)
    intervals = job_intervals(on.schedule, on)
    assert intervals["kodi"] == 60 and intervals["kodi_scan"] == 5
    assert {"kodi", "kodi_scan", "subtitle_sync", "trakt_sync"} <= set(_JOBS)


async def test_an_empty_path_map_is_a_note_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch, path_map=False)

    with pytest.raises(NotConfiguredError, match="path_map"):
        await kodi.sync_history(config)


async def test_an_unknown_timezone_is_a_clear_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch, timezone="Mars/Olympus")

    with pytest.raises(NotConfiguredError, match="Mars/Olympus"):
        await kodi.sync_history(config)
