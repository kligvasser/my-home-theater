"""Execution pipeline: live activity feed + manual stage triggers (auth-gated)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..config import effective_config, get_config
from ..errors import JobBusyError, redact_exc
from ..locks import is_job_running, job_lock
from ..logging_setup import get_logger
from ..pipeline import activity
from .auth import require_token

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["pipeline"])


def _window_info(cfg: Any) -> dict[str, Any]:
    w = cfg.acquisition.window
    hour = datetime.now().hour  # local wall-clock; "night" is local
    return {
        "enabled": w.enabled,
        "start_hour": w.start_hour,
        "end_hour": w.end_hour,
        "is_open": w.is_open(hour),
    }


def _public(value: Any) -> Any:
    """``value`` without local filesystem paths: this feed is open to the whole LAN,
    and nothing on the page uses them."""

    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items() if k != "save_path"}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


@router.get("/activity")
async def api_activity() -> dict[str, Any]:
    """Live state of every in-flight candidate + the acquire window status."""

    # effective_config() reads runtime overrides from the DB — off the loop.
    cfg = await asyncio.to_thread(effective_config)
    states = await activity(cfg)
    return {"window": _window_info(cfg), "items": [_public(s.as_dict()) for s in states]}


def _busy(lock: str, label: str) -> dict[str, Any] | None:
    """The response for a trigger whose job is already running, if it is.

    Only a courtesy: the job itself takes the lock, so losing a race here means the
    background run logs a skip. Reported as ``started: false`` so the dashboard
    doesn't claim something began.
    """

    if not is_job_running(get_config(), lock):
        return None
    return {"started": False, "message": f"A {label} is already running — it'll finish on its own."}


@router.post("/pipeline/acquire-now", dependencies=[Depends(require_token)])
async def api_acquire_now(background: BackgroundTasks) -> dict[str, Any]:
    """Grab every approved candidate right now (bypasses the nightly window)."""

    from ..acquisition import queue_approved

    if busy := _busy("acquire", "grab"):
        return busy

    async def _run() -> None:
        try:
            await queue_approved(await asyncio.to_thread(effective_config))
        except Exception as exc:  # already recorded in job_run
            log.warning("acquire_now.failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True}


@router.post("/pipeline/sync", dependencies=[Depends(require_token)])
async def api_sync(background: BackgroundTasks) -> dict[str, Any]:
    """Advance in-flight downloads now (poll the client, import completed ones)."""

    from ..acquisition import sync_downloads

    if busy := _busy("sync", "sync"):
        return busy

    async def _run() -> None:
        try:
            await sync_downloads(await asyncio.to_thread(effective_config))
        except Exception as exc:
            log.warning("sync_now.failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True, "message": "Advancing downloads…"}


@router.post("/pipeline/enrich", dependencies=[Depends(require_token)])
def api_enrich(background: BackgroundTasks) -> dict[str, Any]:
    """Backfill TMDb/IMDb ids, ratings, genres and taste features for catalog
    titles that are missing them — the data the dashboard/insights display."""

    if busy := _busy("enrich", "metadata refresh"):
        return busy

    def _run() -> None:
        try:
            from ..metadata.service import enrich_catalog

            cfg = effective_config()
            with job_lock(cfg, "enrich"):
                asyncio.run(enrich_catalog(cfg))
        except Exception as exc:
            log.warning("enrich_now.failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True}


@router.post("/pipeline/scan", dependencies=[Depends(require_token)])
def api_scan(background: BackgroundTasks) -> dict[str, Any]:
    """Rescan the NAS now so freshly-imported files enter the catalog (→ subs)."""

    if busy := _busy("scan", "scan"):
        return busy

    def _run() -> None:
        try:
            from ..scanner import run_scan

            run_scan(effective_config())
        except Exception as exc:
            log.warning("scan_now.failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True}


@router.get("/cleanup", dependencies=[Depends(require_token)])
async def api_cleanup_plan() -> dict[str, Any]:
    """Dry-run cleanup report: NAS extras + stuck torrents that could be removed."""

    from ..cleanup import plan_cleanup

    try:
        plan = await plan_cleanup(get_config())
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=redact_exc(exc)) from exc
    d = plan.as_dict()
    d["extras_count"] = len(d["extras"])
    d["stuck_count"] = len(d["stuck"])
    d["unmanaged_count"] = len(d["unmanaged"])
    return d


class CleanupApply(BaseModel):
    """What the reviewer saw in the preview. Both fields are required, so a POST
    can't mean "whatever you find now"."""

    extras: list[str]
    hashes: list[str]


@router.post("/cleanup", dependencies=[Depends(require_token)])
async def api_cleanup_apply(reviewed: CleanupApply) -> dict[str, Any]:
    """Remove the reviewed items that a fresh plan still marks as removable."""

    from ..cleanup import apply_cleanup

    try:
        return await apply_cleanup(
            get_config(), extra_paths=reviewed.extras, infohashes=reviewed.hashes
        )
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=redact_exc(exc)) from exc
