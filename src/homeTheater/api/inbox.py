"""The attention inbox: the page, its data, the nav badge and the accept-removal path."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import __version__
from ..config import AppConfig, effective_config
from ..errors import redact_exc
from ..health import check_all
from ..health.checks import cached_statuses
from ..inbox import collect, latest_prune_refusals
from ..locks import is_import_active, is_job_running
from ..logging_setup import get_logger
from .auth import require_token
from .templates import templates

log = get_logger(__name__)

router = APIRouter(tags=["inbox"])


@router.get("/inbox", response_class=HTMLResponse, include_in_schema=False)
def inbox_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "inbox.html", {"active": "inbox", "version": __version__}
    )


@router.get("/api/inbox")
async def api_inbox() -> dict[str, Any]:
    cfg = await asyncio.to_thread(effective_config)
    providers = await check_all(cfg)
    return (await asyncio.to_thread(collect, cfg, providers)).as_dict()


@router.get("/api/inbox/count")
def api_inbox_count() -> dict[str, int]:
    """For the nav badge on every page: never probes, so providers are as of the last
    time something did."""

    return {"count": collect(effective_config(), cached_statuses()).total}


class AcceptRemoval(BaseModel):
    """The refusal the owner reviewed: which root, and which exact set of rows."""

    root: str
    fingerprint: str


def _force_prune(cfg: AppConfig, root: str, fingerprint: str) -> None:
    from ..scanner import run_scan

    try:
        run_scan(cfg, force_prune=True, accepted_prunes={root: fingerprint})
    except Exception as exc:  # already in job_run, and the inbox still shows it
        log.warning("inbox.force_prune_failed", root=root, error=redact_exc(exc))


@router.post("/api/inbox/force-prune", dependencies=[Depends(require_token)])
def api_force_prune(accepted: AcceptRemoval, background: BackgroundTasks) -> dict[str, Any]:
    """Rescan, letting the reviewed prune through. Goes through ``run_scan``, so it
    holds the scan lock; a walk that finds a different set refuses again."""

    cfg = effective_config()
    if is_job_running(cfg, "scan"):
        raise HTTPException(409, "A scan is already running. Try again when it's done.")
    if is_import_active(cfg):  # the NAS throws EIO when a walk overlaps an import
        raise HTTPException(409, "An import is copying to the NAS. Try again when it's done.")
    if not any(
        r["root"] == accepted.root and r["fingerprint"] == accepted.fingerprint
        for r in latest_prune_refusals()
    ):
        raise HTTPException(409, "That refusal is out of date. Review the inbox again.")
    background.add_task(_force_prune, cfg, accepted.root, accepted.fingerprint)
    return {
        "started": True,
        "message": "Rescanning. The removal goes through if the same files are still missing.",
    }
