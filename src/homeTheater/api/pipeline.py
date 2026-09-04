"""Execution pipeline: live activity feed + manual stage triggers (auth-gated)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends

from ..config import effective_config, get_config
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


@router.get("/activity")
async def api_activity() -> dict[str, Any]:
    """Live state of every in-flight candidate + the acquire window status."""

    import asyncio

    # effective_config() reads runtime overrides from the DB — off the loop.
    cfg = await asyncio.to_thread(effective_config)
    states = await activity(cfg)
    return {"window": _window_info(cfg), "items": [s.as_dict() for s in states]}


@router.post("/pipeline/acquire-now", dependencies=[Depends(require_token)])
async def api_acquire_now(background: BackgroundTasks) -> dict[str, Any]:
    """Grab every approved candidate right now (bypasses the nightly window)."""

    from ..acquisition import queue_approved

    async def _run() -> None:
        try:
            await queue_approved(get_config())
        except Exception as exc:  # already recorded in job_run
            from ..errors import redact_exc

            log.warning("acquire_now.failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True}


@router.post("/pipeline/sync", dependencies=[Depends(require_token)])
async def api_sync(background: BackgroundTasks) -> dict[str, Any]:
    """Advance in-flight downloads now (poll the client, import completed ones)."""

    from ..acquisition import sync_downloads

    async def _run() -> None:
        try:
            await sync_downloads(get_config())
        except Exception as exc:
            from ..errors import redact_exc

            log.warning("sync_now.failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True}


@router.post("/pipeline/enrich", dependencies=[Depends(require_token)])
def api_enrich(background: BackgroundTasks) -> dict[str, Any]:
    """Backfill TMDb/IMDb ids, ratings, genres and taste features for catalog
    titles that are missing them — the data the dashboard/insights display."""

    def _run() -> None:
        try:
            import asyncio

            from ..config import get_config
            from ..metadata.service import enrich_catalog

            asyncio.run(enrich_catalog(get_config()))
        except Exception as exc:
            from ..errors import redact_exc

            log.warning("enrich_now.failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True}


@router.post("/pipeline/scan", dependencies=[Depends(require_token)])
def api_scan(background: BackgroundTasks) -> dict[str, Any]:
    """Rescan the NAS now so freshly-imported files enter the catalog (→ subs)."""

    def _run() -> None:
        try:
            from ..scanner import build_filesystem, config_roots, scan_library

            cfg = get_config()
            scan_library(build_filesystem(cfg), config_roots(cfg))
        except Exception as exc:
            from ..errors import redact_exc

            log.warning("scan_now.failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True}


@router.get("/cleanup", dependencies=[Depends(require_token)])
async def api_cleanup_plan() -> dict[str, Any]:
    """Dry-run cleanup report: NAS extras + stuck torrents that could be removed."""

    from ..cleanup import plan_cleanup

    plan = await plan_cleanup(get_config())
    d = plan.as_dict()
    # Trim the extras list for the UI (full delete re-derives it server-side).
    d["extras_sample"] = [{"path": e["path"], "size": e["size"]} for e in d["extras"][:50]]
    d["extras_count"] = len(d["extras"])
    d["stuck_count"] = len(d["stuck"])
    del d["extras"]
    return d


@router.post("/cleanup", dependencies=[Depends(require_token)])
async def api_cleanup_apply() -> dict[str, Any]:
    """Perform cleanup now: delete NAS extras and remove stuck/imported torrents."""

    from ..cleanup import apply_cleanup

    return await apply_cleanup(get_config())
