"""``job_run`` bookkeeping shared by both subtitle backends, off the event loop."""

from __future__ import annotations

import asyncio
from typing import Any

from ..db.base import utcnow
from ..db.models import JobRun, RunStatus
from ..db.session import session_scope

DB_TIMEOUT_S = 30.0
MAX_ERRORS_SHOWN = 5


def _start_run() -> int:
    with session_scope() as session:
        run = JobRun(kind="subtitle", started_at=utcnow(), status=RunStatus.running)
        session.add(run)
        session.flush()
        return run.id


def _finish_run(run_id: int, status: RunStatus, stats: dict[str, Any]) -> None:
    with session_scope() as session:
        job_run = session.get(JobRun, run_id)
        if job_run is not None:
            job_run.finished_at = utcnow()
            job_run.status = status
            job_run.stats = stats


async def start_run() -> int:
    return await asyncio.wait_for(asyncio.to_thread(_start_run), DB_TIMEOUT_S)


async def finish_run(run_id: int, status: RunStatus, stats: dict[str, Any]) -> None:
    """Record the outcome. Shielded: this also runs while the sweep is being
    cancelled, and a run must not stay ``running`` because of a second cancel."""

    await asyncio.shield(
        asyncio.wait_for(asyncio.to_thread(_finish_run, run_id, status, stats), DB_TIMEOUT_S)
    )
