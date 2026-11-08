"""Phase 9 hardening: provider health, status surfacing, DB backup."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


@respx.mock
async def test_health_checks(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "k")  # only TMDb configured
    _reset()
    from homeTheater.config import get_config
    from homeTheater.health import check_all

    respx.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=httpx.Response(200, json={})
    )

    by_name = {s.name: s for s in await check_all(get_config())}
    assert by_name["tmdb"].configured and by_name["tmdb"].ok is True
    assert by_name["radarr"].configured is False  # not set -> not probed
    assert by_name["smb"].ok is None  # configured-only, never probed


@respx.mock
async def test_health_reports_unreachable(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "k")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.health import check_all

    respx.get("https://api.themoviedb.org/3/configuration").mock(return_value=httpx.Response(401))
    by_name = {s.name: s for s in await check_all(get_config())}
    assert by_name["tmdb"].configured and by_name["tmdb"].ok is False


@respx.mock
async def test_health_detail_redacts_api_key(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx errors embed the request URL (key included); detail must not."""

    monkeypatch.setenv("TMDB_API_KEY", "sekret123")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.health import check_all

    respx.get("https://api.themoviedb.org/3/configuration").mock(return_value=httpx.Response(401))
    by_name = {s.name: s for s in await check_all(get_config())}
    tmdb = by_name["tmdb"]
    assert tmdb.ok is False
    assert "sekret123" not in tmdb.detail
    assert "api_key=REDACTED" in tmdb.detail


@respx.mock
async def test_health_results_are_cached(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unauthenticated status hits must not probe providers on every call."""

    monkeypatch.setenv("TMDB_API_KEY", "k")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.health import check_all

    route = respx.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=httpx.Response(200, json={})
    )
    first = await check_all(get_config())
    second = await check_all(get_config())
    assert route.call_count == 1
    assert first == second


# --- S17: NAS mount and quota probes ------------------------------------------


def _mount_config(tmp_path: Path, **subtitles: object) -> object:
    from homeTheater.config.settings import AppConfig, NASPaths, Subtitles

    return AppConfig(
        nas=NASPaths(share="T", movies_root="M", tv_root="TV"),
        subtitles=Subtitles(backend="native", **subtitles),  # type: ignore[arg-type]
    )


def _fake_mount_table(monkeypatch: pytest.MonkeyPatch, *mounts: str) -> None:
    from homeTheater.health import checks

    async def table() -> list[str]:
        return list(mounts)

    monkeypatch.setattr(checks, "_mount_points", table)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_healthy_mount_is_written_and_left_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.health import check_nas_mount

    _fake_mount_table(monkeypatch, "/", str(tmp_path))
    status = await check_nas_mount(_mount_config(tmp_path, library_base_dir=str(tmp_path)))
    assert status.ok is True and "mounted at" in status.detail
    assert list(tmp_path.iterdir()) == []


async def test_wedged_mount_is_down_and_its_probe_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged SMB mount listed ok: the mount table still shows it. Only a real
    write finds out, and that write has to be killable."""

    from homeTheater.health import check_nas_mount, checks

    pid_file = tmp_path / "pid"
    monkeypatch.setattr(
        checks,
        "_WRITE_PROBE",
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)",
    )
    monkeypatch.setattr(checks, "_WRITE_PROBE_TIMEOUT_SECONDS", 0.5)
    _fake_mount_table(monkeypatch, str(tmp_path))
    status = await check_nas_mount(_mount_config(tmp_path, library_base_dir=str(tmp_path)))
    assert status.ok is False and "wedged" in status.detail
    assert not _alive(int(pid_file.read_text()))


async def test_read_only_mount_is_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.health import check_nas_mount

    tmp_path.chmod(0o500)
    try:
        _fake_mount_table(monkeypatch, str(tmp_path))
        status = await check_nas_mount(_mount_config(tmp_path, library_base_dir=str(tmp_path)))
    finally:
        tmp_path.chmod(0o700)
    assert status.ok is False and "not writable" in status.detail


async def test_stale_volumes_folder_is_down_and_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an unmount, /Volumes/X can survive as a plain folder. It listed as a
    fine "local dir"; a write would land on the boot disk."""

    from homeTheater.health import check_nas_mount, checks

    async def no_probe(base: str) -> int:
        raise AssertionError("probed a stale mount point")

    monkeypatch.setattr(checks, "_write_probe", no_probe)
    _fake_mount_table(monkeypatch, "/", "/Volumes/Other")
    status = await check_nas_mount(_mount_config(tmp_path, library_base_dir="/Volumes/Gone"))
    assert status.ok is False and "NOT mounted" in status.detail


async def test_subfolder_of_a_mount_counts_as_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.health import check_nas_mount, checks

    async def probe_ok(base: str) -> int:
        return 0

    monkeypatch.setattr(checks, "_write_probe", probe_ok)
    _fake_mount_table(monkeypatch, "/", "/Volumes/My Share")
    status = await check_nas_mount(
        _mount_config(tmp_path, library_base_dir="/Volumes/My Share/Media")
    )
    assert status.ok is True and "mounted at" in status.detail


async def test_plain_local_dir_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.health import check_nas_mount

    _fake_mount_table(monkeypatch, "/")
    status = await check_nas_mount(_mount_config(tmp_path, library_base_dir=str(tmp_path)))
    assert status.ok is True and "local dir" in status.detail
    missing = _mount_config(tmp_path, library_base_dir=str(tmp_path / "nope"))
    assert (await check_nas_mount(missing)).ok is False


async def test_hung_mount_command_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.health import check_nas_mount, checks

    pid_file = tmp_path / "pid"
    fake = tmp_path / "mount"
    fake.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(checks, "_MOUNT_TIMEOUT_SECONDS", 3.0)
    status = await check_nas_mount(_mount_config(tmp_path, library_base_dir=str(tmp_path)))
    assert status.ok is None and "unknown" in status.detail
    assert not _alive(int(pid_file.read_text()))


def test_mount_table_parsing_handles_spaces_in_names() -> None:
    from homeTheater.health import checks

    table = (
        "/dev/disk3s1 on / (apfs, sealed)\n"
        "//guest@nas/Elements on /Volumes/Elements 25A1 (smbfs, nodev)\n"
    )
    assert checks._MOUNT_LINE_RE.findall(table) == ["/", "/Volumes/Elements 25A1"]


@respx.mock
async def test_nas_mount_is_checked_when_only_subtitles_use_it(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production ran arr-less subtitles through the mount with no check at all."""

    from homeTheater.config.settings import AppConfig, NASPaths, Subtitles
    from homeTheater.health import check_all

    cfg = AppConfig(
        nas=NASPaths(share="T", movies_root="M", tv_root="TV"),
        subtitles=Subtitles(backend="native", library_base_dir="/Volumes/Gone"),
    )
    _fake_mount_table(monkeypatch)
    by_name = {s.name: s for s in await check_all(cfg)}
    assert cfg.acquisition.backend == "arr"
    assert by_name["nas-mount"].ok is False


def _opensubtitles_routes(monkeypatch: pytest.MonkeyPatch, remaining: int) -> tuple[object, object]:
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "k")
    monkeypatch.setenv("OPENSUBTITLES_USERNAME", "u")
    monkeypatch.setenv("OPENSUBTITLES_PASSWORD", "p")
    base = "https://api.opensubtitles.com/api/v1"
    login = respx.post(f"{base}/login").mock(
        return_value=httpx.Response(200, json={"token": "tok"})
    )
    info = respx.get(f"{base}/infos/user").mock(
        return_value=httpx.Response(
            200, json={"data": {"remaining_downloads": remaining, "allowed_downloads": 20}}
        )
    )
    return login, info


def _native_config() -> object:
    from homeTheater.config.settings import AppConfig, NASPaths, Secrets, Subtitles

    return AppConfig(
        nas=NASPaths(share="T", movies_root="M", tv_root="TV"),
        subtitles=Subtitles(backend="native"),
        secrets=Secrets(),
    )


@respx.mock
async def test_opensubtitles_quota_zero_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.health import check_opensubtitles

    _opensubtitles_routes(monkeypatch, remaining=0)
    status = await check_opensubtitles(_native_config())
    assert status.ok is False and status.detail == "0/20 downloads left today"


@respx.mock
async def test_opensubtitles_token_survives_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logins are rate-limited; every Status refresh used to do one."""

    from homeTheater.health import check_opensubtitles

    login, info = _opensubtitles_routes(monkeypatch, remaining=5)
    first = await check_opensubtitles(_native_config())
    second = await check_opensubtitles(_native_config())
    assert first.ok is True and second.ok is True
    assert login.call_count == 1 and info.call_count == 2  # type: ignore[attr-defined]


@respx.mock
async def test_rejected_opensubtitles_token_is_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.health import check_opensubtitles

    login, info = _opensubtitles_routes(monkeypatch, remaining=5)
    await check_opensubtitles(_native_config())
    info.mock(return_value=httpx.Response(401))  # type: ignore[attr-defined]
    assert (await check_opensubtitles(_native_config())).ok is False
    info.mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json={"data": {"remaining_downloads": 5}})
    )
    assert (await check_opensubtitles(_native_config())).ok is True
    assert login.call_count == 2  # type: ignore[attr-defined]


# --- S17/W19: /health and /ready -------------------------------------------------


def _enable_schedule(config_file: Path, **intervals: int) -> None:
    lines = "".join(f"  {k}: {v}\n" for k, v in intervals.items())
    with config_file.open("a") as f:
        f.write(f"schedule:\n  enabled: false\n{lines}")


def test_health_reports_stale_jobs_and_stays_200(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import JobRun, RunStatus

    _reset()
    init_db()
    old = datetime.now(UTC) - timedelta(days=12)
    with session_scope() as s:
        s.add(JobRun(kind="backup", status=RunStatus.success, started_at=old, finished_at=old))

    from homeTheater.api import create_app
    from homeTheater.config import get_config

    with TestClient(create_app()) as client:
        assert client.get("/health").json()["status"] == "ok"  # scheduler off: nothing due
        cfg = get_config().model_copy(deep=True)
        cfg.schedule.enabled = True
        monkeypatch.setattr("homeTheater.api.health.get_config", lambda: cfg)
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded" and "backup" in r.json()["stale_jobs"]


def test_ready_is_a_generic_503_when_the_database_fails(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    from homeTheater.api import create_app

    def broken() -> object:
        raise RuntimeError("unable to open sqlite:////Users/me/secret-dir/x.db?password=hunter2")

    with TestClient(create_app()) as client:
        monkeypatch.setattr("homeTheater.api.health.get_engine", broken)
        r = client.get("/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "database": "error"}


def test_status_lists_per_job_freshness(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    init_db()
    cfg = get_config().model_copy(deep=True)
    cfg.schedule.enabled = True
    monkeypatch.setattr("homeTheater.api.status.effective_config", lambda: cfg)
    monkeypatch.setattr("homeTheater.api.web.effective_config", lambda: cfg)
    with TestClient(create_app()) as client:
        jobs = {j["name"]: j for j in client.get("/api/status").json()["jobs"]}
        page = client.get("/status")
    assert jobs["backup"]["interval_minutes"] == 1440 and jobs["backup"]["stale"] is True
    assert "Scheduled jobs" in page.text and "never" in page.text


def test_startup_reaps_orphaned_runs(config_file: Path) -> None:
    from datetime import UTC, datetime

    from homeTheater.api import create_app
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import JobRun, RunStatus

    _reset()
    init_db()
    with session_scope() as s:
        s.add(JobRun(kind="scan", status=RunStatus.running, started_at=datetime.now(UTC)))
    with TestClient(create_app()):
        pass
    with session_scope() as s:
        assert s.query(JobRun).one().status is RunStatus.failed


def test_backup_creates_valid_snapshot(config_file: Path, tmp_path: Path) -> None:
    _reset()
    from homeTheater.backup import backup_database
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title, TitleKind

    init_db()
    with session_scope() as s:
        s.add(Title(title="The Matrix", year=1999, kind=TitleKind.movie))

    dest = backup_database(get_config(), dest_dir=tmp_path / "bk", keep=3)
    assert dest.exists()

    # The snapshot is a valid SQLite DB containing our data.
    conn = sqlite3.connect(str(dest))
    try:
        count = conn.execute("SELECT COUNT(*) FROM title").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_backup_rejects_non_sqlite(config_file: Path) -> None:
    _reset()
    from homeTheater.backup import backup_database
    from homeTheater.config.settings import AppConfig, Database, NASPaths

    cfg = AppConfig(
        nas=NASPaths(share="T", movies_root="M", tv_root="TV"),
        database=Database(url="postgresql://x/y"),
    )
    with pytest.raises(ValueError, match="SQLite"):
        backup_database(cfg)


def test_status_endpoint_and_page(config_file: Path) -> None:
    # No provider creds set -> check_all makes no network calls.
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        providers = client.get("/api/providers").json()["providers"]
        names = {p["name"] for p in providers}
        assert {"tmdb", "omdb", "radarr", "sonarr", "bazarr", "smb"} <= names

        status = client.get("/api/status").json()
        assert status["dry_run"] is True and status["scheduler"] is False

        page = client.get("/status")
        assert page.status_code == 200 and "Status" in page.text


@respx.mock
@pytest.mark.parametrize(
    ("key", "in_url", "in_header"),
    [("v3hexkey", True, False), ("aaa.bbb.ccc", False, True)],
)
async def test_tmdb_health_authenticates_like_the_client(
    key: str, in_url: bool, in_header: bool
) -> None:
    from types import SimpleNamespace

    from pydantic import SecretStr

    from homeTheater.health.checks import check_tmdb

    route = respx.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=httpx.Response(200, json={})
    )
    config = SimpleNamespace(secrets=SimpleNamespace(tmdb_api_key=SecretStr(key)))

    status = await check_tmdb(config)  # type: ignore[arg-type]

    sent = route.calls.last.request
    assert status.ok is True
    assert ("api_key" in str(sent.url)) is in_url
    assert ("authorization" in sent.headers) is in_header
