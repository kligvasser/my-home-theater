"""Build the APScheduler that runs the periodic pipeline (plan §5.9).

Each job is individually toggleable: an interval of 0 disables that job.
``max_instances=1`` + ``coalesce`` (plus the global lock in :mod:`.jobs`) keep
jobs from piling up.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import AppConfig
from ..logging_setup import get_logger
from .jobs import (
    run_acquire_job,
    run_backup_job,
    run_discovery_job,
    run_reconcile_job,
    run_scan_job,
    run_subtitle_job,
    run_sync_job,
)

log = get_logger(__name__)

# Jobs queue behind the global run lock, so a fire can legitimately start late;
# don't let APScheduler's default 1s grace silently skip it.
MISFIRE_GRACE_SECONDS = 300


def build_scheduler(config: AppConfig) -> AsyncIOScheduler:
    sched = AsyncIOScheduler()
    s = config.schedule

    jobs = [
        ("scan", s.scan_interval_minutes, run_scan_job),
        ("discovery", s.discovery_interval_minutes, run_discovery_job),
        ("subtitle", s.subtitle_interval_minutes, run_subtitle_job),
        ("acquire", s.acquire_interval_minutes, run_acquire_job),
        ("sync", s.sync_interval_minutes, run_sync_job),
        ("reconcile", s.import_reconcile_interval_minutes, run_reconcile_job),
        ("backup", s.backup_interval_minutes, run_backup_job),
    ]
    for job_id, minutes, func in jobs:
        if minutes <= 0:
            continue
        sched.add_job(
            func,
            "interval",
            minutes=minutes,
            id=job_id,
            args=[config],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        log.info("scheduler.registered", job=job_id, minutes=minutes)
    return sched
