"""Keep periodic jobs honest on a host that sleeps.

``APScheduler`` counts intervals from process start and knows nothing about the
past, so a restart resets every clock and a sleeping Mac misses its fires. Here the
``job_run`` table is the record of what actually happened: at startup and after a
wake, any job whose last success is older than its interval runs now. The same
record feeds per-job freshness on the Status page and ``/health``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select

from ..config import AppConfig
from ..config.settings import Schedule
from ..db.base import utcnow
from ..db.models import JobRun, RunStatus
from ..db.session import session_scope
from ..errors import redact_exc
from ..locks import is_job_running
from ..logging_setup import get_logger
from . import jobs

log = get_logger(__name__)

# ``partial`` (a sweep that hit some errors) still ran.
_SUCCESSFUL = frozenset({"success", "partial"})

# A job that keeps failing mustn't re-fire on every dark wake: after any attempt
# wait this long (or the job's interval, if shorter) before catching it up again.
CATCH_UP_RETRY_MINUTES = 30
# job_run says "stale" past interval × this, plus the slack below.
STALE_FACTOR = 2
STALE_SLACK_MINUTES = 30

STORAGE_NOTE_HOURS = 6

WAKE_POLL_SECONDS = 30.0
# The loop stops with the host, so a poll that took this much longer than it should
# (wall clock) means the machine was asleep.
WAKE_GAP_SECONDS = 120.0

# job_run.kind -> job_lock name, where they differ.
_LOCK_NAMES = {"metadata": "enrich", "subtitle": "subtitles"}


def _optional_jobs(config: AppConfig) -> dict[str, int]:
    """Jobs of features that are off by default: not registered until switched on."""

    out: dict[str, int] = {}
    if config.subtitles.sync.enabled:
        out["subtitle_sync"] = config.subtitles.sync.interval_minutes
    if config.kodi.enabled:
        out["kodi"] = config.kodi.history_interval_minutes
        out["kodi_scan"] = config.kodi.scan_poll_minutes
    if config.trakt.sync.enabled:
        out["trakt_sync"] = config.trakt.sync.interval_minutes
    return out


def job_intervals(schedule: Schedule, config: AppConfig | None = None) -> dict[str, int]:
    """Enabled jobs -> interval in minutes (0 disables a job)."""

    intervals = {
        "scan": schedule.scan_interval_minutes,
        "discovery": schedule.discovery_interval_minutes,
        "subtitle": schedule.subtitle_interval_minutes,
        "acquire": schedule.acquire_interval_minutes,
        "sync": schedule.sync_interval_minutes,
        "reconcile": schedule.import_reconcile_interval_minutes,
        "backup": schedule.backup_interval_minutes,
        "train": schedule.train_interval_minutes,
        "trash": schedule.trash_interval_minutes,
        "upgrades": schedule.upgrades_interval_minutes,
    }
    if config is not None:
        intervals.update(_optional_jobs(config))
    return {name: minutes for name, minutes in intervals.items() if minutes > 0}


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything stored is UTC."""

    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _last_runs() -> tuple[dict[str, datetime], dict[str, datetime]]:
    """Per job kind: (newest finish of a successful run, newest start of any run)."""

    successes: dict[str, datetime] = {}
    attempts: dict[str, datetime] = {}
    with session_scope() as s:
        rows = s.execute(
            select(
                JobRun.kind,
                JobRun.status,
                func.max(JobRun.finished_at),
                func.max(JobRun.started_at),
            ).group_by(JobRun.kind, JobRun.status)
        ).all()
    for kind, status, finished, started in rows:
        finished, started = _aware(finished), _aware(started)
        if started is not None and (kind not in attempts or started > attempts[kind]):
            attempts[kind] = started
        if finished is None or getattr(status, "value", status) not in _SUCCESSFUL:
            continue
        if kind not in successes or finished > successes[kind]:
            successes[kind] = finished
    return successes, attempts


def due_jobs(config: AppConfig, now: datetime | None = None) -> list[str]:
    """Jobs whose last success is older than their interval."""

    now = now or utcnow()
    successes, attempts = _last_runs()
    due: list[str] = []
    for name, minutes in job_intervals(config.schedule, config).items():
        last_success = successes.get(name)
        if last_success is not None and now - last_success < timedelta(minutes=minutes):
            continue
        retry_after = timedelta(minutes=min(minutes, CATCH_UP_RETRY_MINUTES))
        tried = [t for t in (attempts.get(name),) if t is not None]
        if name in jobs.last_attempts:
            tried.append(datetime.fromtimestamp(jobs.last_attempts[name], UTC))
        if tried and now - max(tried) < retry_after:
            continue
        due.append(name)
    if _digest_due(config, now):
        due.append("digest")
    return due


def _digest_due(config: AppConfig, now: datetime) -> bool:
    """A digest isn't an interval job: it's due when today's hasn't gone out. Failed
    attempts wait like any other job's, so an outage can't re-fire it on every wake."""

    from ..digest import digest_due

    last = jobs.last_attempts.get("digest")
    if last is not None and now - datetime.fromtimestamp(last, UTC) < timedelta(
        minutes=CATCH_UP_RETRY_MINUTES
    ):
        return False
    return digest_due(config, now)


def register_clock_jobs(sched: AsyncIOScheduler, config: AppConfig) -> None:
    """Jobs that aren't on ``job_intervals``, so they have no freshness to report.

    The digest fires at a local hour. The storage note is due whenever the app has
    been up a while, so it fires at registration too; a day's second note is dropped.
    """

    common: dict[str, Any] = {
        "args": [config],
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": None,  # a fire is late by however long the host slept
        "replace_existing": True,
    }
    if config.digest.enabled:
        sched.add_job(
            jobs.run_digest_job, "cron", hour=config.digest.hour, minute=0, id="digest", **common
        )
    sched.add_job(
        jobs.run_storage_job,
        "interval",
        hours=STORAGE_NOTE_HOURS,
        id="storage",
        next_run_time=datetime.now(sched.timezone),
        **common,
    )


async def catch_up(sched: AsyncIOScheduler, config: AppConfig, reason: str) -> list[str]:
    """Pull every overdue job's next run to now.

    Going through APScheduler rather than calling the job keeps ``max_instances``
    and ``coalesce`` in charge, so a catch-up can't double a run already under way.
    """

    due = await asyncio.to_thread(due_jobs, config)
    fire_at = datetime.now(sched.timezone)
    fired = [name for name in due if sched.get_job(name) is not None]
    for name in fired:
        sched.modify_job(name, next_run_time=fire_at)
    if fired:
        log.info("scheduler.catch_up", reason=reason, jobs=fired)
    return fired


async def watch_for_wake(sched: AsyncIOScheduler, config: AppConfig) -> None:
    """Run forever, catching jobs up whenever the host comes back from sleep."""

    last = jobs._wall_clock()
    while True:
        await asyncio.sleep(WAKE_POLL_SECONDS)
        now = jobs._wall_clock()
        slept = now - last - WAKE_POLL_SECONDS
        last = now
        if slept < WAKE_GAP_SECONDS:
            continue
        log.info("scheduler.wake", slept_seconds=int(slept))
        try:
            await catch_up(sched, config, "wake")
        except Exception as exc:  # the watcher must outlive one bad read
            log.warning("scheduler.catch_up_failed", error=redact_exc(exc))


def reap_orphaned_runs(config: AppConfig) -> int:
    """Mark ``running`` rows left by a dead process as failed; returns how many.

    Run once at startup. A row whose job lock is held belongs to a live CLI run in
    another process, so it stays.
    """

    now = utcnow()
    reaped = 0
    with session_scope() as s:
        running = s.scalars(select(JobRun).where(JobRun.status == RunStatus.running)).all()
        for run in running:
            if is_job_running(config, _LOCK_NAMES.get(run.kind, run.kind)):
                continue
            run.status = RunStatus.failed
            run.finished_at = now
            run.stats = {**(run.stats or {}), "error": "process exited before the job finished"}
            reaped += 1
    if reaped:
        log.warning("scheduler.reaped_orphaned_runs", count=reaped)
    return reaped


@dataclass(frozen=True, slots=True)
class JobFreshness:
    name: str
    interval_minutes: int
    last_success: str | None  # ISO timestamp
    age_minutes: int | None  # None = no recorded success
    stale: bool
    note: str = ""  # why a job isn't running, when it's skipping


def job_freshness(config: AppConfig, now: datetime | None = None) -> list[JobFreshness]:
    """How long ago each enabled job last succeeded, and whether that's a problem.

    Empty when the scheduler is off: nothing is expected to run. A job that is
    skipping (provider not configured, window closed) is never stale.
    """

    if not config.schedule.enabled:
        return []
    now = now or utcnow()
    successes, _ = _last_runs()
    out: list[JobFreshness] = []
    for name, minutes in job_intervals(config.schedule, config).items():
        last = successes.get(name)
        skip = jobs.skip_reasons.get(name, "")
        limit = timedelta(minutes=minutes * STALE_FACTOR + STALE_SLACK_MINUTES)
        out.append(
            JobFreshness(
                name=name,
                interval_minutes=minutes,
                last_success=last.isoformat() if last else None,
                age_minutes=int((now - last).total_seconds() // 60) if last else None,
                stale=not skip and (last is None or now - last > limit),
                note=skip,
            )
        )
    return out
