"""Scheduler job registration, resource locks, deadlines and catch-up."""

from __future__ import annotations

import asyncio
import errno
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from homeTheater.config import AppConfig
from homeTheater.config.settings import NASPaths, Schedule
from homeTheater.db.models import JobRun, RunStatus


def _config(**schedule_kwargs: object) -> AppConfig:
    schedule_kwargs.setdefault("trash_interval_minutes", 0)
    schedule_kwargs.setdefault("upgrades_interval_minutes", 0)
    return AppConfig(
        nas=NASPaths(share="T", movies_root="M", tv_root="TV"),
        schedule=Schedule(**schedule_kwargs),  # type: ignore[arg-type]
    )


def test_build_scheduler_respects_intervals() -> None:
    from homeTheater.scheduler import build_scheduler

    cfg = _config(
        scan_interval_minutes=0,  # disabled
        discovery_interval_minutes=30,
        subtitle_interval_minutes=0,  # disabled
        acquire_interval_minutes=0,  # disabled
        sync_interval_minutes=5,
        import_reconcile_interval_minutes=0,  # disabled
        backup_interval_minutes=0,  # disabled
        train_interval_minutes=0,
    )
    sched = build_scheduler(cfg)
    assert {j.id for j in sched.get_jobs()} == {"discovery", "sync"}


def test_build_scheduler_registers_acquire_job() -> None:
    from homeTheater.scheduler import build_scheduler
    from homeTheater.scheduler.scheduler import MISFIRE_GRACE_SECONDS

    cfg = _config(
        scan_interval_minutes=0,
        discovery_interval_minutes=0,
        subtitle_interval_minutes=0,
        acquire_interval_minutes=30,
        sync_interval_minutes=0,
        import_reconcile_interval_minutes=0,
        backup_interval_minutes=0,
        train_interval_minutes=0,
    )
    sched = build_scheduler(cfg)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"acquire"}
    # However long the host slept, a missed fire runs once on wake rather than being
    # dropped: None is APScheduler's "no limit", and coalesce folds the misses.
    assert MISFIRE_GRACE_SECONDS is None
    assert jobs["acquire"].misfire_grace_time is None
    assert jobs["acquire"].coalesce is True


async def test_jobs_with_no_declared_resource_stay_serialized(config_file: Path) -> None:
    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    active = 0
    max_seen = 0

    async def body() -> None:
        nonlocal active, max_seen
        active += 1
        max_seen = max(max_seen, active)
        await asyncio.sleep(0.02)
        active -= 1
        return None

    cfg = get_config()
    await asyncio.gather(
        jobs._guarded("a", cfg, body),
        jobs._guarded("b", cfg, body),
        jobs._guarded("c", cfg, body),
    )
    # The lock must have serialized them: never more than one at a time.
    assert max_seen == 1


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _db_config() -> AppConfig:
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    _reset()
    init_db()
    return get_config().model_copy(deep=True)


def _run_row(kind: str, status: RunStatus, age: timedelta, **fields: Any) -> JobRun:
    when = datetime.now(UTC) - age
    return JobRun(kind=kind, status=status, started_at=when, finished_at=when, **fields)


def _add(*rows: JobRun) -> None:
    from homeTheater.db import session_scope

    with session_scope() as s:
        s.add_all(rows)


async def _no_notify(config: AppConfig, text: str) -> None:
    pass


def _collect_notes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from homeTheater.scheduler import jobs

    sent: list[str] = []

    async def fake_notify(config: AppConfig, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)
    return sent


async def test_sync_and_backup_do_not_wait_for_a_subtitle_sweep(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production: one job holding the global lock starved the others for hours."""

    from homeTheater.scheduler import jobs

    cfg = _db_config()
    monkeypatch.setattr(jobs, "notify", _no_notify)
    release = asyncio.Event()
    ran: list[str] = []

    async def sweep() -> None:
        await release.wait()
        ran.append("subtitle")

    async def quick(name: str) -> None:
        ran.append(name)

    long_job = asyncio.create_task(jobs._guarded("subtitle", cfg, sweep))
    await asyncio.sleep(0)
    await asyncio.wait_for(jobs._guarded("sync", cfg, lambda: quick("sync")), 2)
    await asyncio.wait_for(jobs._guarded("backup", cfg, lambda: quick("backup")), 2)
    assert ran == ["sync", "backup"]
    release.set()
    await long_job


async def test_jobs_sharing_a_resource_still_serialize(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import jobs

    cfg = _db_config()
    monkeypatch.setattr(jobs, "notify", _no_notify)
    active = max_seen = 0

    async def body() -> None:
        nonlocal active, max_seen
        active += 1
        max_seen = max(max_seen, active)
        await asyncio.sleep(0.02)
        active -= 1

    await asyncio.gather(jobs._guarded("sync", cfg, body), jobs._guarded("acquire", cfg, body))
    assert max_seen == 1


async def test_deadline_follows_the_wall_clock_not_loop_time(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop's clock stops during sleep. Time that passed while the Mac slept must
    count, or a job hung over a night of dark wakes never times out."""

    from homeTheater.scheduler import jobs

    sent = _collect_notes(monkeypatch)
    monkeypatch.setattr(jobs, "DEADLINE_POLL_SECONDS", 0.01)
    now = [1000.0]
    monkeypatch.setattr(jobs, "_wall_clock", lambda: now[0])
    cfg = _config(job_timeout_minutes=60)
    cancelled = asyncio.Event()

    async def hung() -> None:
        now[0] += 3 * 3600  # a long sleep passes; almost no loop time does
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    await asyncio.wait_for(jobs._guarded("x", cfg, hung), 2)
    assert cancelled.is_set()
    assert len(sent) == 1 and "timed out" in sent[0]


async def test_real_etimedout_is_a_failure_not_a_job_timeout(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError(ETIMEDOUT) is a TimeoutError in Python. Treating it as the watchdog
    firing would report "cancelled after 180 min" for a job that failed in seconds."""

    from homeTheater.scheduler import jobs

    sent = _collect_notes(monkeypatch)

    async def nas_timed_out() -> None:
        raise OSError(errno.ETIMEDOUT, "Operation timed out")

    await jobs._guarded("x", _config(job_timeout_minutes=60), nas_timed_out)
    assert len(sent) == 1
    assert "job failed" in sent[0] and "Operation timed out" in sent[0]


async def test_timeout_unsticks_the_worker_even_with_a_blocked_executor(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the task only abandons a thread. on_timeout is what reaches it,
    and it can't depend on the executor those stuck threads are filling."""

    from homeTheater.scheduler import jobs

    monkeypatch.setattr(jobs, "notify", _no_notify)
    monkeypatch.setattr(jobs, "DEADLINE_POLL_SECONDS", 0.01)
    unstuck = threading.Event()
    cfg = _config()
    cfg.schedule.job_timeout_minutes = 0.001  # type: ignore[assignment]

    def stuck() -> None:
        unstuck.wait(5)

    async def body() -> None:
        await asyncio.to_thread(stuck)

    await jobs._guarded("x", cfg, body, on_timeout=unstuck.set)
    assert unstuck.wait(2)


async def test_a_timed_out_scan_aborts_its_walk(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scanner import ScanStats
    from homeTheater.scheduler import jobs

    monkeypatch.setattr(jobs, "notify", _no_notify)
    monkeypatch.setattr(jobs, "DEADLINE_POLL_SECONDS", 0.01)
    aborted = threading.Event()

    def stuck_scan(*a: object, **k: object) -> ScanStats:
        aborted.wait(5)
        return ScanStats()

    monkeypatch.setattr("homeTheater.scanner.run_scan", stuck_scan)
    monkeypatch.setattr("homeTheater.scanner.abort_active_walks", aborted.set)
    cfg = _db_config()
    cfg.schedule.job_timeout_minutes = 0.001  # type: ignore[assignment]
    await jobs.run_scan_job(cfg)
    assert aborted.wait(2)


@pytest.mark.parametrize("job", ["run_sync_job", "run_subtitle_job"])
async def test_a_timed_out_nas_job_resets_the_smb_pool(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, job: str
) -> None:
    from homeTheater.scheduler import jobs

    monkeypatch.setattr(jobs, "notify", _no_notify)
    monkeypatch.setattr(jobs, "DEADLINE_POLL_SECONDS", 0.01)
    reset = threading.Event()

    async def hang(*a: object, **k: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr("homeTheater.acquisition.sync_downloads", hang)
    monkeypatch.setattr("homeTheater.subtitles.sweep_subtitles", hang)
    monkeypatch.setattr("homeTheater.scanner.reset_smb_pool", reset.set)
    cfg = _db_config()
    cfg.schedule.job_timeout_minutes = 0.001  # type: ignore[assignment]
    await getattr(jobs, job)(cfg)
    assert reset.wait(2)


@pytest.mark.parametrize("enabled", [True, False])
def test_startup_catches_up_and_watches_for_wake(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from fastapi.testclient import TestClient

    from homeTheater.api import create_app

    with config_file.open("a") as f:
        f.write(f"schedule:\n  enabled: true\n  catch_up: {str(enabled).lower()}\n")
    _reset()
    calls: list[str] = []

    async def fake_catch_up(sched: object, config: AppConfig, reason: str) -> list[str]:
        calls.append(reason)
        return []

    async def fake_watch(sched: object, config: AppConfig) -> None:
        calls.append("watching")

    monkeypatch.setattr("homeTheater.scheduler.catch_up", fake_catch_up)
    monkeypatch.setattr("homeTheater.scheduler.watch_for_wake", fake_watch)
    with TestClient(create_app()):
        pass
    assert calls == (["startup", "watching"] if enabled else [])


@pytest.mark.parametrize(
    ("runner", "service", "section"),
    [
        ("run_scan_job", "homeTheater.scanner.run_scan", "sync"),
        ("run_discovery_job", "homeTheater.discovery.run_discovery", "async"),
        ("run_subtitle_job", "homeTheater.subtitles.sweep_subtitles", "async"),
        ("run_acquire_job", "homeTheater.acquisition.queue_approved", "async"),
        ("run_sync_job", "homeTheater.acquisition.sync_downloads", "async"),
        ("run_reconcile_job", "homeTheater.reconcile.reconcile_library", "async"),
        ("run_backup_job", "homeTheater.backup.backup_database", "sync"),
    ],
)
async def test_every_job_body_reads_overrides_per_run(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, runner: str, service: str, section: str
) -> None:
    """Dashboard overrides were ignored by the jobs that matter: each received the
    config frozen at startup."""

    from homeTheater.config import save_overrides
    from homeTheater.scheduler import jobs

    startup_cfg = _db_config()
    monkeypatch.setattr(jobs, "notify", _no_notify)
    save_overrides({"subtitles": {"max_searches_per_sweep": 7}})
    seen: list[int] = []

    class Stats:
        files_added = created = queued = completed = imported = 0

        def as_dict(self) -> dict[str, int]:
            return {}

    def record(cfg: AppConfig, *a: object, **k: object) -> Stats:
        seen.append(cfg.subtitles.max_searches_per_sweep)
        return Stats()

    async def record_async(cfg: AppConfig, *a: object, **k: object) -> Stats:
        return record(cfg)

    monkeypatch.setattr(service, record if section == "sync" else record_async)
    await getattr(jobs, runner)(startup_cfg)
    assert startup_cfg.subtitles.max_searches_per_sweep == 50
    assert seen == [7]


async def test_acquire_job_honours_a_window_override(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import save_overrides
    from homeTheater.scheduler import jobs

    startup_cfg = _db_config()
    monkeypatch.setattr(jobs, "notify", _no_notify)
    hour = datetime.now().hour
    closed = {"enabled": True, "start_hour": (hour + 2) % 24, "end_hour": (hour + 3) % 24}
    save_overrides({"acquisition": {"window": closed}})

    async def must_not_run(*a: object) -> None:
        raise AssertionError("grabbed outside the overridden window")

    monkeypatch.setattr("homeTheater.acquisition.queue_approved", must_not_run)
    await jobs.run_acquire_job(startup_cfg)
    assert "window" in jobs.skip_reasons["acquire"]


async def test_sync_and_backup_leave_a_job_run_row(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They wrote none, so nothing could tell a 12-day backup gap from a healthy one."""

    from homeTheater.db import session_scope
    from homeTheater.scheduler import jobs

    cfg = _db_config()
    monkeypatch.setattr(jobs, "notify", _no_notify)

    async def ok() -> None:
        return None

    async def boom() -> None:
        raise RuntimeError("no space")

    await jobs._guarded("backup", cfg, ok)
    await jobs._guarded("sync", cfg, boom)
    with session_scope() as s:
        rows = {r.kind: r for r in s.query(JobRun).all()}
        assert rows["backup"].status is RunStatus.success and rows["backup"].finished_at
        assert rows["sync"].status is RunStatus.failed
        assert rows["sync"].stats == {"error": "no space"}


async def test_uneventful_syncs_collapse_into_one_row(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """144 no-op rows a day would push real runs off the dashboard."""

    from homeTheater.db import session_scope
    from homeTheater.scheduler import jobs

    cfg = _db_config()
    monkeypatch.setattr(jobs, "notify", _no_notify)

    async def quiet() -> None:
        return None

    async def imported() -> str:
        return "imported 1"

    await jobs._guarded("sync", cfg, quiet)
    await jobs._guarded("sync", cfg, quiet)
    await jobs._guarded("sync", cfg, imported)
    await jobs._guarded("sync", cfg, quiet)
    with session_scope() as s:
        assert s.query(JobRun).filter_by(kind="sync").count() == 3


def test_due_jobs_reads_job_run_history(config_file: Path) -> None:
    from homeTheater.scheduler.catchup import due_jobs

    cfg = _db_config()
    cfg.schedule = Schedule(
        trash_interval_minutes=0,
        upgrades_interval_minutes=0,
        scan_interval_minutes=360,
        discovery_interval_minutes=720,
        subtitle_interval_minutes=720,
        acquire_interval_minutes=0,
        sync_interval_minutes=0,
        import_reconcile_interval_minutes=0,
        backup_interval_minutes=1440,
        train_interval_minutes=0,
    )
    _add(
        _run_row("scan", RunStatus.success, timedelta(minutes=30)),  # fresh
        _run_row("backup", RunStatus.success, timedelta(days=12)),  # the production gap
        _run_row("subtitle", RunStatus.partial, timedelta(minutes=30)),  # partial still ran
    )
    assert due_jobs(cfg) == ["discovery", "backup"]  # discovery has never run


def test_a_failing_job_is_not_refired_on_every_dark_wake(config_file: Path) -> None:
    from homeTheater.scheduler.catchup import CATCH_UP_RETRY_MINUTES, due_jobs

    cfg = _db_config()
    cfg.schedule = Schedule(
        trash_interval_minutes=0,
        upgrades_interval_minutes=0,
        scan_interval_minutes=360,
        discovery_interval_minutes=0,
        subtitle_interval_minutes=0,
        acquire_interval_minutes=0,
        sync_interval_minutes=0,
        import_reconcile_interval_minutes=0,
        backup_interval_minutes=0,
        train_interval_minutes=0,
    )
    _add(
        _run_row("scan", RunStatus.success, timedelta(days=2)),
        _run_row("scan", RunStatus.failed, timedelta(minutes=5)),
    )
    assert due_jobs(cfg) == []
    later = datetime.now(UTC) + timedelta(minutes=CATCH_UP_RETRY_MINUTES)
    assert due_jobs(cfg, now=later) == ["scan"]


def test_a_skipped_job_is_not_refired_on_every_dark_wake(config_file: Path) -> None:
    """A skip writes no job_run row, so only the in-memory attempt can hold it off."""

    from homeTheater.scheduler import jobs
    from homeTheater.scheduler.catchup import due_jobs

    cfg = _db_config()
    cfg.schedule = Schedule(
        trash_interval_minutes=0,
        upgrades_interval_minutes=0,
        scan_interval_minutes=0,
        discovery_interval_minutes=0,
        subtitle_interval_minutes=0,
        acquire_interval_minutes=30,
        sync_interval_minutes=0,
        import_reconcile_interval_minutes=0,
        backup_interval_minutes=0,
        train_interval_minutes=0,
    )
    assert due_jobs(cfg) == ["acquire"]
    jobs.last_attempts["acquire"] = time.time()
    assert due_jobs(cfg) == []


async def test_catch_up_runs_overdue_jobs_at_startup(config_file: Path) -> None:
    """A restart resets APScheduler's interval clocks, so a process restarted daily
    never reached its first backup."""

    from homeTheater.scheduler import build_scheduler, catch_up

    cfg = _db_config()
    cfg.schedule = Schedule(
        trash_interval_minutes=0,
        upgrades_interval_minutes=0,
        scan_interval_minutes=360,
        discovery_interval_minutes=0,
        subtitle_interval_minutes=0,
        acquire_interval_minutes=0,
        sync_interval_minutes=0,
        import_reconcile_interval_minutes=0,
        backup_interval_minutes=1440,
        train_interval_minutes=0,
    )
    _add(
        _run_row("scan", RunStatus.success, timedelta(minutes=1)),
        _run_row("backup", RunStatus.success, timedelta(days=12)),
    )
    sched = build_scheduler(cfg)
    sched.start(paused=True)
    try:
        before = datetime.now(sched.timezone) + timedelta(seconds=5)
        assert await catch_up(sched, cfg, "startup") == ["backup"]
        assert sched.get_job("backup").next_run_time <= before
        assert sched.get_job("scan").next_run_time > before
    finally:
        sched.shutdown(wait=False)


async def test_wake_watcher_catches_up_after_a_sleep_gap(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import catchup, jobs

    monkeypatch.setattr(catchup, "WAKE_POLL_SECONDS", 0.001)
    now = [0.0]

    def clock() -> float:
        now[0] += 0.001  # normal ticks...
        return now[0]

    monkeypatch.setattr(jobs, "_wall_clock", clock)
    reasons: list[str] = []
    woke = asyncio.Event()

    async def fake_catch_up(sched: object, config: AppConfig, reason: str) -> list[str]:
        reasons.append(reason)
        woke.set()
        return []

    monkeypatch.setattr(catchup, "catch_up", fake_catch_up)
    watcher = asyncio.create_task(catchup.watch_for_wake(object(), _config()))  # type: ignore[arg-type]
    await asyncio.sleep(0.02)
    assert reasons == []  # ...aren't a wake
    now[0] += 8 * 3600  # the lid was shut overnight
    await asyncio.wait_for(woke.wait(), 2)
    watcher.cancel()
    assert reasons == ["wake"]


def test_startup_reaper_fails_orphans_and_spares_live_cli_runs(config_file: Path) -> None:
    """Production has `running` rows from dead processes that never resolve."""

    from homeTheater.db import session_scope
    from homeTheater.locks import job_lock
    from homeTheater.scheduler import reap_orphaned_runs

    cfg = _db_config()
    orphan = JobRun(
        kind="scan",
        status=RunStatus.running,
        started_at=datetime.now(UTC),
        stats={"files_scanned": 3},
    )
    live = JobRun(kind="discovery", status=RunStatus.running, started_at=datetime.now(UTC))
    _add(orphan, live)

    with job_lock(cfg, "discovery"):  # a CLI run in "another process"
        assert reap_orphaned_runs(cfg) == 1

    with session_scope() as s:
        rows = {r.kind: r for r in s.query(JobRun).all()}
        assert rows["scan"].status is RunStatus.failed and rows["scan"].finished_at
        assert rows["scan"].stats["files_scanned"] == 3
        assert "exited" in rows["scan"].stats["error"]
        assert rows["discovery"].status is RunStatus.running


def test_freshness_flags_stale_jobs_but_not_skipping_ones(config_file: Path) -> None:
    from homeTheater.scheduler import job_freshness, jobs

    cfg = _db_config()
    cfg.schedule = Schedule(
        trash_interval_minutes=0,
        upgrades_interval_minutes=0,
        enabled=True,
        scan_interval_minutes=0,
        discovery_interval_minutes=0,
        subtitle_interval_minutes=0,
        acquire_interval_minutes=30,
        sync_interval_minutes=10,
        import_reconcile_interval_minutes=60,
        backup_interval_minutes=1440,
        train_interval_minutes=0,
    )
    _add(
        _run_row("backup", RunStatus.success, timedelta(days=12)),
        _run_row("sync", RunStatus.success, timedelta(minutes=3)),
    )
    jobs.skip_reasons["reconcile"] = "RADARR_URL is not set"
    jobs.skip_reasons["acquire"] = "download window is closed"
    by_name = {j.name: j for j in job_freshness(cfg)}
    assert by_name["backup"].stale and by_name["backup"].age_minutes == 12 * 24 * 60
    assert not by_name["sync"].stale and by_name["sync"].age_minutes == 3
    assert not by_name["reconcile"].stale and by_name["reconcile"].last_success is None
    assert not by_name["acquire"].stale


def test_freshness_is_empty_with_the_scheduler_off(config_file: Path) -> None:
    from homeTheater.scheduler import job_freshness

    assert job_freshness(_db_config()) == []


async def test_guarded_swallows_failure(config_file: Path) -> None:
    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    async def boom() -> None:
        raise RuntimeError("kaboom")

    # Must not propagate — a failing job can't kill the scheduler.
    await jobs._guarded("x", get_config(), boom)


async def test_guarded_skips_quietly_on_not_configured(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NotConfiguredError is the expected 'provider missing' skip — no alert."""

    from homeTheater.config import get_config
    from homeTheater.errors import NotConfiguredError
    from homeTheater.scheduler import jobs

    sent: list[str] = []

    async def fake_notify(config: AppConfig, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)

    async def not_configured() -> str | None:
        raise NotConfiguredError("RADARR_API_KEY is not set")

    await jobs._guarded("acquire", get_config(), not_configured)
    assert sent == []


async def test_guarded_alerts_on_plain_value_error(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine ValueError is a real failure, not a quiet config skip."""

    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    sent: list[str] = []

    async def fake_notify(config: AppConfig, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)

    async def buggy() -> str | None:
        raise ValueError("unexpected payload shape")

    await jobs._guarded("discovery", get_config(), buggy)
    assert len(sent) == 1 and "failed" in sent[0]


async def test_guarded_redacts_secrets_in_failure_notification(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    sent: list[str] = []

    async def fake_notify(config: AppConfig, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)

    async def leaky() -> str | None:
        raise RuntimeError(
            "Client error '401 Unauthorized' for url "
            "'https://api.themoviedb.org/3/configuration?api_key=sekret123'"
        )

    await jobs._guarded("discovery", get_config(), leaky)
    assert len(sent) == 1
    assert "sekret123" not in sent[0]
    assert "api_key=REDACTED" in sent[0]


async def test_scan_job_skips_quietly_while_a_scan_holds_the_lock(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import job_lock
    from homeTheater.scheduler import jobs

    get_config.cache_clear()
    sent: list[str] = []

    async def fake_notify(config: AppConfig, text: str) -> None:
        sent.append(text)

    def no_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("scan ran without the lock")

    monkeypatch.setattr(jobs, "notify", fake_notify)
    monkeypatch.setattr("homeTheater.scanner.scan_library", no_scan)
    monkeypatch.setattr("homeTheater.scanner.build_filesystem", lambda cfg: object())

    cfg = get_config()
    with job_lock(cfg, "scan"):
        await jobs.run_scan_job(cfg)
    assert sent == []


async def test_scan_lock_outlives_a_timed_out_job(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wait_for abandons the thread, not the walk: the lock must stay held until
    the walk really ends, or the next scan runs alongside the orphan."""

    from homeTheater.config import get_config
    from homeTheater.locks import is_job_running
    from homeTheater.scanner import ScanStats
    from homeTheater.scheduler import jobs

    get_config.cache_clear()
    release = threading.Event()

    def stuck_scan(*args: object, **kwargs: object) -> ScanStats:
        release.wait(5)
        return ScanStats()

    async def fake_notify(config: AppConfig, text: str) -> None:
        pass

    monkeypatch.setattr(jobs, "notify", fake_notify)
    monkeypatch.setattr("homeTheater.scanner.scan_library", stuck_scan)
    monkeypatch.setattr("homeTheater.scanner.build_filesystem", lambda cfg: object())

    cfg = get_config().model_copy(deep=True)
    cfg.schedule.job_timeout_minutes = 0.001  # type: ignore[assignment]
    await jobs.run_scan_job(cfg)
    try:
        assert is_job_running(cfg, "scan")
    finally:
        release.set()
    for _ in range(100):
        if not is_job_running(cfg, "scan"):
            break
        await asyncio.sleep(0.01)
    assert not is_job_running(cfg, "scan")


def _reset_train_env() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


async def test_train_job_retrains_and_records_a_run(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L16: a model nobody retrains goes stale."""

    _reset_train_env()
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.preferences import TrainStats
    from homeTheater.scheduler import jobs

    init_db()
    calls: list[object] = []

    def fake_train(cfg: object) -> TrainStats:
        calls.append(cfg)
        return TrainStats(trained=True)

    monkeypatch.setattr("homeTheater.preferences.train", fake_train)
    await jobs.run_train_job(get_config())

    assert len(calls) == 1
    with session_scope() as s:
        assert s.scalars(select(JobRun).where(JobRun.kind == "train")).one().status is (
            RunStatus.success
        )


async def test_train_job_with_too_few_labels_is_a_skip(config_file: Path) -> None:
    _reset_train_env()
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.scheduler import jobs

    init_db()
    await jobs.run_train_job(get_config())  # empty DB: nothing to learn from

    assert "not enough decisions" in jobs.skip_reasons["train"]
    with session_scope() as s:
        assert s.scalars(select(JobRun).where(JobRun.kind == "train")).first() is None


def test_train_job_is_scheduled_by_default() -> None:
    from homeTheater.scheduler import jobs
    from homeTheater.scheduler.catchup import job_intervals
    from homeTheater.scheduler.scheduler import _JOBS

    assert job_intervals(Schedule())["train"] == 1440
    assert _JOBS["train"] is jobs.run_train_job
    assert jobs.JOB_RESOURCES["train"] != jobs.JOB_RESOURCES["discovery"]
