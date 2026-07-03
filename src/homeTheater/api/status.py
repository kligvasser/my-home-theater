"""Status/health surfacing: provider reachability + recent failures."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter

from ..config import get_config
from ..dashboard import recent_runs
from ..health import check_all

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/providers")
async def api_providers() -> dict[str, Any]:
    statuses = await check_all(get_config())
    return {"providers": [asdict(s) for s in statuses]}


@router.get("/status")
async def api_status() -> dict[str, Any]:
    cfg = get_config()
    statuses = await check_all(cfg)
    # recent_runs is sync SQLAlchemy; keep it off the event loop.
    runs = await asyncio.to_thread(recent_runs, 50)
    failed = [asdict(r) for r in runs if r.status == "failed"]
    return {
        "dry_run": cfg.features.dry_run,
        "auto_approve": cfg.features.auto_approve,
        "scheduler": cfg.schedule.enabled,
        "providers": [asdict(s) for s in statuses],
        "recent_failures": failed,
    }
