"""Scheduled job wrappers (plan §5.9).

Jobs that touch the same resource run one at a time, so periodic jobs never
stampede a backend or overlap destructively, while unrelated jobs carry on: an
hours-long subtitle sweep must not hold up sync or the backup. Each job is wrapped
so a failure is logged and notified rather than killing the scheduler.

Deadlines are wall-clock. ``asyncio`` timers run on the monotonic clock, which stops
while the Mac sleeps, so a job hung across a night of sleep/dark-wake cycles would
never reach a loop-time timeout.
"""

from __future__ import annotations

import asyncio
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select

from ..config import AppConfig, ConfigError, effective_config
from ..db.base import utcnow
from ..db.models import JobRun, RunStatus
from ..db.session import session_scope
from ..errors import JobBusyError, JobDeadlineExceededError, NotConfiguredError, redact_exc
from ..logging_setup import bind_run, clear_run, get_logger
from ..notifications import notify
from ..notifications.alerts import report_job

log = get_logger(__name__)

# Which lock each job takes. Sync, acquire and reconcile all move candidates and
# downloads through their states, so they share one; the rest stand alone. A job
# not listed here shares "default", which keeps a new job serialized until someone
# decides what it touches.
JOB_RESOURCES = {
    "scan": "scan",
    "subtitle": "subtitles",
    "discovery": "discovery",
    "backup": "backup",
    "train": "train",
    "sync": "downloads",
    "acquire": "downloads",
    "reconcile": "downloads",
    "trash": "downloads",  # takes the sync lock, so it can't overlap an import
    "upgrades": "upgrades",
    "digest": "digest",
    "storage": "storage",
    "subtitle_sync": "subtitles",  # rewrites subtitle files, so never beside a sweep
    "kodi": "kodi",
    "kodi_scan": "kodi",
    "trakt_sync": "trakt",
}
_DEFAULT_RESOURCE = "default"

# The other jobs write their own job_run row; these would otherwise leave no record,
# and catch-up and /health need one.
GUARD_RECORDED = frozenset(
    {
        "sync",
        "backup",
        "train",
        "digest",
        "trash",
        "upgrades",
        "subtitle_sync",
        "kodi",
        "kodi_scan",
        "trakt_sync",
    }
)

# How often a running job's deadline is checked against the wall clock.
DEADLINE_POLL_SECONDS = 30.0
# How long a cancelled job gets to clean up before it is left behind.
CANCEL_GRACE_SECONDS = 30.0

# asyncio.Lock binds to a loop on first contention, so keep one set per loop.
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)

# Why each job last did nothing, for /health: a job skipped because it has nothing to
# do (provider not configured, window closed) isn't stale, however old its last
# success. In memory on purpose: startup catch-up re-establishes it.
skip_reasons: dict[str, str] = {}
# Wall-clock time of each job's last attempt, skips included (see catchup.due_jobs).
last_attempts: dict[str, float] = {}


class JobSkippedError(RuntimeError):
    """The job has nothing to do right now. Not a failure and not worth an alert."""


def _wall_clock() -> float:
    return time.time()


def _lock_for(name: str) -> asyncio.Lock:
    resource = JOB_RESOURCES.get(name, _DEFAULT_RESOURCE)
    locks = _locks.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(resource, asyncio.Lock())


async def _run_with_deadline(
    name: str,
    body: Callable[[], Awaitable[str | None]],
    timeout: float,
    on_timeout: Callable[[], None] | None,
) -> str | None:
    """Await ``body`` until ``timeout`` wall-clock seconds pass, then cancel it.

    Raises :class:`JobDeadlineExceededError` on expiry. A ``TimeoutError`` raised by
    ``body`` itself (a real ``ETIMEDOUT`` from the NAS) passes through as the
    ordinary failure it is. ``on_timeout`` unsticks whatever a worker thread is
    blocked on, since cancelling the task only abandons the thread.
    """

    task = asyncio.ensure_future(body())
    # Late exceptions from a job we stopped waiting for aren't "never retrieved".
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    deadline = _wall_clock() + timeout
    try:
        while (remaining := deadline - _wall_clock()) > 0:
            done, _ = await asyncio.wait({task}, timeout=min(remaining, DEADLINE_POLL_SECONDS))
            if done:
                return task.result()
    except asyncio.CancelledError:  # app shutting down
        task.cancel()
        raise

    if on_timeout is not None:
        # Not to_thread: stuck workers may be why we're here, and the executor is
        # finite. This has to run even if every worker is blocked.
        threading.Thread(target=on_timeout, name=f"{name}-timeout", daemon=True).start()
    task.cancel()
    _, pending = await asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS)
    if pending:
        log.error("job.cancel_ignored", job=name)
    raise JobDeadlineExceededError(name)


def _record_run(
    name: str, started: datetime, status: RunStatus, stats: dict[str, str | None]
) -> None:
    """Write the job_run row for a job that doesn't write its own.

    An uneventful success refreshes the previous one instead of adding a row: sync
    fires every 10 minutes, and 144 identical rows a day would push everything else
    off the Runs page.
    """

    with session_scope() as s:
        latest = s.scalars(
            select(JobRun).where(JobRun.kind == name).order_by(JobRun.started_at.desc()).limit(1)
        ).first()
        quiet = status is RunStatus.success and not stats.get("summary")
        previous_quiet = (
            latest is not None
            and latest.status is RunStatus.success
            and not (latest.stats or {}).get("summary")
        )
        if latest is not None and quiet and previous_quiet:
            latest.started_at = started
            latest.finished_at = utcnow()
            return
        s.add(
            JobRun(kind=name, started_at=started, finished_at=utcnow(), status=status, stats=stats)
        )


async def _guarded(
    name: str,
    config: AppConfig,
    body: Callable[[], Awaitable[str | None]],
    *,
    on_timeout: Callable[[], None] | None = None,
) -> None:
    """Run one job under its resource lock, logging + notifying on failure.

    The notification is sent *after* the lock is released so a slow Telegram
    round-trip never delays the next job.
    """

    message: str | None = None
    status: RunStatus | None = None
    stats: dict[str, str | None] = {}
    async with _lock_for(name):
        last_attempts[name] = _wall_clock()
        started = utcnow()
        bind_run(job=name, scheduled=True)
        try:
            log.info("job.start", job=name)
            timeout = config.schedule.job_timeout_minutes * 60
            if timeout:
                summary = await _run_with_deadline(name, body, timeout, on_timeout)
            else:
                summary = await body()
            log.info("job.done", job=name, summary=summary)
            message = summary
            skip_reasons.pop(name, None)
            status, stats = RunStatus.success, {"summary": summary}
        except JobDeadlineExceededError:
            # A hung job (stuck NAS mount, wedged HTTP) is cancelled so it releases
            # its lock — otherwise everything sharing the resource stops firing.
            mins = config.schedule.job_timeout_minutes
            log.error("job.timeout", job=name, minutes=mins)
            message = f"⚠️ {name} job timed out after {mins} min and was cancelled"
            status, stats = RunStatus.failed, {"error": f"timed out after {mins} min"}
        except (ConfigError, NotConfiguredError, JobBusyError, JobSkippedError) as exc:
            # Expected when a provider isn't configured yet, or the same job is
            # running from the CLI/dashboard — skip quietly.
            reason = redact_exc(exc)
            log.info("job.skipped", job=name, reason=reason)
            skip_reasons[name] = reason
        except Exception as exc:
            error = redact_exc(exc)
            log.error("job.failed", job=name, error=error)
            message = f"⚠️ {name} job failed: {error}"
            skip_reasons.pop(name, None)
            status, stats = RunStatus.failed, {"error": error}
        finally:
            clear_run()
    if status is not None and name in GUARD_RECORDED:
        try:
            await asyncio.to_thread(_record_run, name, started, status, stats)
        except Exception as exc:  # history is not worth failing a finished job over
            log.warning("job.record_failed", job=name, error=redact_exc(exc))
    if status is not None:  # a skip proves nothing, so it neither alerts nor recovers
        failed = status is RunStatus.failed
        await report_job(config, name, failed=failed, message=message, send=notify)


async def _config_for_run() -> AppConfig:
    """File config + dashboard overrides, read per run so they apply without a
    restart. The ``config`` a job is registered with is frozen at startup."""

    return await asyncio.to_thread(effective_config)


async def run_scan_job(config: AppConfig) -> None:
    from ..locks import SCAN_IMPORT_WAIT_S, wait_for_import_gap
    from ..scanner import abort_active_walks, run_scan

    async def body() -> str | None:
        cfg = await _config_for_run()
        # The NAS throws EIO when work overlaps an import, and a walk can wait.
        if not await wait_for_import_gap(cfg, SCAN_IMPORT_WAIT_S):
            raise JobSkippedError("a NAS import is copying; scan deferred")
        stats = await asyncio.to_thread(run_scan, cfg)
        return None if stats.files_added == 0 else f"📀 Scan added {stats.files_added} file(s)"

    # run_scan holds the scan lock in its thread, so a timed-out walk blocks the
    # next scan until it has really ended. Aborting it makes that soon.
    await _guarded("scan", config, body, on_timeout=abort_active_walks)


async def run_discovery_job(config: AppConfig) -> None:
    from ..discovery import run_discovery

    async def body() -> str | None:
        stats = await run_discovery(await _config_for_run())
        return f"🍿 {stats.created} new candidate(s)" if stats.created else None

    await _guarded("discovery", config, body)


async def run_subtitle_job(config: AppConfig) -> None:
    from ..scanner import reset_smb_pool
    from ..subtitles import sweep_subtitles

    async def body() -> str | None:
        stats = (await sweep_subtitles(await _config_for_run())).as_dict()
        # native backend reports downloads; bazarr reports triggered searches
        n = stats.get("downloaded") or (
            stats.get("searched_movies", 0) + stats.get("searched_episodes", 0)
        )
        return f"💬 {n} subtitle(s)" if n else None

    await _guarded("subtitle", config, body, on_timeout=reset_smb_pool)


async def run_acquire_job(config: AppConfig) -> None:
    from ..acquisition import queue_approved

    async def body() -> str | None:
        cfg = await _config_for_run()
        # Respect the nightly download window on the *scheduled* path only; a manual
        # "grab now" (CLI/dashboard) calls queue_approved directly and bypasses this.
        window = cfg.acquisition.window
        if not window.is_open(datetime.now().hour):
            log.info("acquire.window_closed", start=window.start_hour, end=window.end_hour)
            raise JobSkippedError("download window is closed")
        stats = await queue_approved(cfg)
        return f"🎯 Queued {stats.queued} approved candidate(s)" if stats.queued else None

    await _guarded("acquire", config, body)


async def run_sync_job(config: AppConfig) -> None:
    from ..acquisition import sync_downloads
    from ..scanner import reset_smb_pool

    async def body() -> str | None:
        stats = await sync_downloads(await _config_for_run())
        return f"✅ {stats.completed} download(s) imported" if stats.completed else None

    await _guarded("sync", config, body, on_timeout=reset_smb_pool)


async def run_reconcile_job(config: AppConfig) -> None:
    from ..reconcile import reconcile_library

    async def body() -> str | None:
        stats = await reconcile_library(await _config_for_run())
        return f"🔄 Reconciled {stats.imported} import(s)" if stats.imported else None

    await _guarded("reconcile", config, body)


async def run_backup_job(config: AppConfig) -> None:
    from ..backup import backup_database

    async def body() -> str | None:
        await asyncio.to_thread(backup_database, await _config_for_run())
        return None  # routine; no need to notify on every backup

    await _guarded("backup", config, body)


async def run_train_job(config: AppConfig) -> None:
    """Retrain the preference model, so it keeps up with your decisions."""

    from ..preferences import train

    async def body() -> str | None:
        stats = await asyncio.to_thread(train, await _config_for_run())
        if not stats.trained:
            raise JobSkippedError(stats.message)
        return None  # routine; no need to notify on every retrain

    await _guarded("train", config, body)


async def run_digest_job(config: AppConfig) -> None:
    """The morning digest. It sends for itself, because it has to know it arrived."""

    from ..digest import send_digest

    async def body() -> str | None:
        await send_digest(await _config_for_run())
        return None

    await _guarded("digest", config, body)


async def run_storage_job(config: AppConfig) -> None:
    """Note NAS usage, once a day at most: the storage planner's growth history."""

    from ..storage import record_snapshot

    async def body() -> str | None:
        if not await record_snapshot(await _config_for_run()):
            raise JobSkippedError("NAS capacity can't be read right now")
        return None

    await _guarded("storage", config, body)


async def run_trash_job(config: AppConfig) -> None:
    """Purge what's past ``trash.retention_days``."""

    from ..trash import purge

    cancel = threading.Event()

    async def body() -> str | None:
        cfg = await _config_for_run()
        if not cfg.trash.enabled:
            raise JobSkippedError("trash.enabled is off")
        stats = await asyncio.to_thread(purge, cfg, cancel=cancel)
        if stats.errors:
            raise RuntimeError(f"trash purge: {'; '.join(stats.errors)}")
        return f"🗑 Purged {stats.purged} trashed file(s)" if stats.purged else None

    await _guarded("trash", config, body, on_timeout=cancel.set)


async def run_upgrades_job(config: AppConfig) -> None:
    """Look for better copies of owned movies. Only proposes; nothing downloads here."""

    from ..upgrades import scan_for_upgrades

    async def body() -> str | None:
        cfg = await _config_for_run()
        if not cfg.upgrades.enabled:
            raise JobSkippedError("upgrades.enabled is off")
        stats = await scan_for_upgrades(cfg)
        return f"⬆️ {stats.proposed} upgrade(s) proposed" if stats.proposed else None

    await _guarded("upgrades", config, body)


async def run_subtitle_sync_job(config: AppConfig) -> None:
    """Timing check. Missing ffsubsync/ffmpeg arrives as a skip, shown on Status."""

    from ..subtitles.sync import run_sync_check

    async def body() -> str | None:
        stats = await run_sync_check(await _config_for_run())
        if stats.deferred and not stats.checked:
            raise JobSkippedError("a NAS import is copying; timing check deferred")
        return f"💬 Retimed {stats.corrected} subtitle(s)" if stats.corrected else None

    await _guarded("subtitle_sync", config, body)


async def run_kodi_job(config: AppConfig) -> None:
    """Read Kodi's watch history. Kodi being off is a skip, not a failure."""

    from ..kodi import KodiUnavailableError, sync_history

    async def body() -> str | None:
        try:
            await sync_history(await _config_for_run())
        except KodiUnavailableError as exc:
            raise JobSkippedError(redact_exc(exc)) from None
        return None

    await _guarded("kodi", config, body)


async def run_kodi_scan_job(config: AppConfig) -> None:
    """Send the debounced library scan, if one is due."""

    from ..kodi import KodiUnavailableError, scan_if_due

    async def body() -> str | None:
        try:
            await scan_if_due(await _config_for_run())
        except KodiUnavailableError as exc:
            raise JobSkippedError(redact_exc(exc)) from None
        return None

    await _guarded("kodi_scan", config, body)


async def run_trakt_sync_job(config: AppConfig) -> None:
    import httpx

    from ..watch import sync_trakt

    async def body() -> str | None:
        try:
            await sync_trakt(await _config_for_run())
        except httpx.HTTPError as exc:  # an outage is Trakt's, and the next run catches up
            raise JobSkippedError(f"Trakt is unavailable: {redact_exc(exc)}") from None
        return None

    await _guarded("trakt_sync", config, body)
