"""Scheduled job wrappers (plan §5.9).

A single module-level lock is the **global concurrency guard**: only one scheduled
job runs at a time, so periodic jobs never stampede the NAS or the arr/metadata
APIs, nor overlap destructively. Each job is wrapped so a failure is logged and
notified rather than killing the scheduler.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..config import AppConfig, ConfigError
from ..errors import NotConfiguredError, redact_exc
from ..logging_setup import bind_run, clear_run, get_logger
from ..notifications import notify

log = get_logger(__name__)

# Global guard: serialize all scheduled jobs.
_run_lock = asyncio.Lock()


async def _guarded(name: str, config: AppConfig, body: Callable[[], Awaitable[str | None]]) -> None:
    """Run one job under the global lock, logging + notifying on failure.

    The notification is sent *after* the lock is released so a slow Telegram
    round-trip never delays the next job.
    """

    message: str | None = None
    async with _run_lock:
        bind_run(job=name, scheduled=True)
        try:
            log.info("job.start", job=name)
            summary = await body()
            log.info("job.done", job=name, summary=summary)
            message = summary
        except (ConfigError, NotConfiguredError) as exc:
            # Expected when a provider isn't configured yet — skip quietly.
            log.info("job.skipped", job=name, reason=str(exc))
        except Exception as exc:
            error = redact_exc(exc)
            log.error("job.failed", job=name, error=error)
            message = f"⚠️ {name} job failed: {error}"
        finally:
            clear_run()
    if message:
        await notify(config, message)


async def run_scan_job(config: AppConfig) -> None:
    from ..scanner import build_filesystem, config_roots, scan_library

    async def body() -> str | None:
        fs = build_filesystem(config)
        stats = await asyncio.to_thread(scan_library, fs, config_roots(config))
        return None if stats.files_added == 0 else f"📀 Scan added {stats.files_added} file(s)"

    await _guarded("scan", config, body)


async def run_discovery_job(config: AppConfig) -> None:
    from ..config import effective_config
    from ..discovery import run_discovery

    async def body() -> str | None:
        # Re-read per run: dashboard runtime overrides (thresholds, sources,
        # taste weight, auto_approve) apply without a restart.
        stats = await run_discovery(effective_config())
        return f"🍿 {stats.created} new candidate(s)" if stats.created else None

    await _guarded("discovery", config, body)


async def run_subtitle_job(config: AppConfig) -> None:
    from ..subtitles import sweep_missing

    async def body() -> str | None:
        stats = await sweep_missing(config)
        searched = stats.searched_movies + stats.searched_episodes
        return f"💬 Requested {searched} subtitle search(es)" if searched else None

    await _guarded("subtitle", config, body)


async def run_acquire_job(config: AppConfig) -> None:
    from ..acquisition import queue_approved

    async def body() -> str | None:
        stats = await queue_approved(config)
        return f"🎯 Queued {stats.queued} approved candidate(s)" if stats.queued else None

    await _guarded("acquire", config, body)


async def run_sync_job(config: AppConfig) -> None:
    from ..acquisition import sync_downloads

    async def body() -> str | None:
        stats = await sync_downloads(config)
        return f"✅ {stats.completed} download(s) imported" if stats.completed else None

    await _guarded("sync", config, body)


async def run_reconcile_job(config: AppConfig) -> None:
    from ..reconcile import reconcile_library

    async def body() -> str | None:
        stats = await reconcile_library(config)
        return f"🔄 Reconciled {stats.imported} import(s)" if stats.imported else None

    await _guarded("reconcile", config, body)


async def run_backup_job(config: AppConfig) -> None:
    from ..backup import backup_database

    async def body() -> str | None:
        await asyncio.to_thread(backup_database, config)
        return None  # routine; no need to notify on every backup

    await _guarded("backup", config, body)
