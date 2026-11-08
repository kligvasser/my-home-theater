"""Build the APScheduler that runs the periodic pipeline (plan §5.9).

Each job is individually toggleable: an interval of 0 disables that job.
``max_instances=1`` + ``coalesce`` (plus the resource locks in :mod:`.jobs`) keep
jobs from piling up, and :mod:`.catchup` makes up for fires lost to sleep or restarts.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import AppConfig
from ..logging_setup import get_logger
from .catchup import job_intervals
from .jobs import (
    run_acquire_job,
    run_backup_job,
    run_discovery_job,
    run_kodi_job,
    run_kodi_scan_job,
    run_reconcile_job,
    run_scan_job,
    run_subtitle_job,
    run_subtitle_sync_job,
    run_sync_job,
    run_train_job,
    run_trakt_sync_job,
    run_trash_job,
    run_upgrades_job,
)

log = get_logger(__name__)

_JOBS = {
    "scan": run_scan_job,
    "discovery": run_discovery_job,
    "subtitle": run_subtitle_job,
    "acquire": run_acquire_job,
    "sync": run_sync_job,
    "reconcile": run_reconcile_job,
    "backup": run_backup_job,
    "train": run_train_job,
    "subtitle_sync": run_subtitle_sync_job,
    "kodi": run_kodi_job,
    "kodi_scan": run_kodi_scan_job,
    "trakt_sync": run_trakt_sync_job,
    "trash": run_trash_job,
    "upgrades": run_upgrades_job,
}

# No grace limit: a fire is late by however long the host slept, and "too late" would
# drop it. A daily job missed by more than a fixed window would otherwise wait a whole
# new interval. ``coalesce`` folds all the fires missed in one sleep into a single run.
MISFIRE_GRACE_SECONDS = None


def build_scheduler(config: AppConfig) -> AsyncIOScheduler:
    sched = AsyncIOScheduler()
    for job_id, minutes in job_intervals(config.schedule, config).items():
        sched.add_job(
            _JOBS[job_id],
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
