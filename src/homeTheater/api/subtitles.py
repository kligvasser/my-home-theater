"""Subtitle coverage (read) + sweep trigger (auth-gated)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel

from ..config import AppConfig, effective_config
from ..dashboard import get_stats, list_missing_subtitles
from ..errors import JobBusyError, NotConfiguredError, redact_exc
from ..logging_setup import get_logger
from ..subtitles import sweep_subtitles
from ..subtitles.review import (
    ReviewConflictError,
    ReviewNotFoundError,
    approve,
    list_candidates,
    list_review,
    revert,
    swap,
)
from ..subtitles.sync import run_sync_check, unavailable_reason
from .auth import require_token

log = get_logger(__name__)

router = APIRouter(prefix="/api/subtitles", tags=["subtitles"])

# A sweep outlives the request that started it; hold a reference so the event loop
# can't garbage-collect the task mid-run.
_background: set[asyncio.Task[None]] = set()


@router.get("/coverage")
def api_coverage() -> dict[str, Any]:
    cov = get_stats(sub_lang=effective_config().subtitles.primary).coverage
    return {"lang": cov.lang, "covered": cov.covered, "total": cov.total, "pct": cov.pct}


@router.get("/missing")
def api_missing() -> dict[str, Any]:
    lang = effective_config().subtitles.primary
    rows = list_missing_subtitles(lang=lang)
    return {"lang": lang, "count": len(rows), "items": [asdict(r) for r in rows]}


async def _run_sweep(config: AppConfig, started: asyncio.Future[None]) -> None:
    """Resolve ``started`` once the sweep is really underway, or with whatever
    stopped it from starting; later failures are recorded on its ``job_run``."""

    def mark_started() -> None:
        if not started.done():
            started.set_result(None)

    try:
        await sweep_subtitles(config, on_started=mark_started)
    except Exception as exc:
        if not started.done():
            started.set_exception(exc)
        else:
            log.error("subtitle.background_failed", error=redact_exc(exc))
    finally:
        if not started.done():
            started.cancel()


@router.post("/search", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_token)])
async def api_search() -> dict[str, Any]:
    """Start a sweep for missing target-language subtitles (Bazarr or native backend).

    A sweep can take many minutes, so it runs in the background: 202 means it
    started, and its outcome appears on the Runs page.
    """

    config = await asyncio.to_thread(effective_config)
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_run_sweep(config, started))
    _background.add(task)
    task.add_done_callback(_background.discard)
    try:
        await started
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A subtitle sweep is already running.") from exc
    except NotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=redact_exc(exc)) from exc
    return {"status": "started", "message": "Subtitle sweep started; watch Runs for the result."}


LangPath = Annotated[str, Path(pattern=r"^[a-z]{2,8}$")]


class SwapRequest(BaseModel):
    candidate: str


@contextmanager
def _review_errors() -> Iterator[None]:
    try:
        yield
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such subtitle.") from exc
    except (ReviewConflictError, JobBusyError) as exc:
        raise HTTPException(status_code=409, detail=redact_exc(exc)) from exc
    except NotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=redact_exc(exc)) from exc


@router.get("/review")
def api_review(
    lang: str | None = None,
    state: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Chosen subtitles and their timing state; by default the ones wanting a look."""

    config = effective_config()
    items = list_review(lang=lang, state=state, q=q, limit=limit)
    return {
        "enabled": config.subtitles.sync.enabled,
        "note": unavailable_reason(config),
        "items": items,
    }


async def _run_check(config: AppConfig, started: asyncio.Future[None]) -> None:
    def mark_started() -> None:
        if not started.done():
            started.set_result(None)

    try:
        await run_sync_check(config, on_started=mark_started)
    except Exception as exc:
        if not started.done():
            started.set_exception(exc)
        else:
            log.error("subtitle.sync_background_failed", error=redact_exc(exc))
    finally:
        if not started.done():
            started.cancel()


@router.post(
    "/sync-check", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_token)]
)
async def api_sync_check() -> dict[str, Any]:
    """Start a timing check now. It can take many minutes, so it runs in the background."""

    config = await asyncio.to_thread(effective_config)
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_run_check(config, started))
    _background.add(task)
    task.add_done_callback(_background.discard)
    with _review_errors():
        await started
    return {"status": "started", "message": "Timing check started."}


@router.post("/files/{owned_file_id}/{lang}/candidates", dependencies=[Depends(require_token)])
async def api_candidates(owned_file_id: int, lang: LangPath) -> dict[str, Any]:
    """Search the providers for this file. Gated: it spends provider quota."""

    config = await asyncio.to_thread(effective_config)
    with _review_errors():
        return {"items": await list_candidates(config, owned_file_id, lang)}


@router.post("/files/{owned_file_id}/{lang}/approve", dependencies=[Depends(require_token)])
def api_approve(owned_file_id: int, lang: LangPath) -> dict[str, Any]:
    with _review_errors():
        approve(effective_config(), owned_file_id, lang)
    return {"status": "approved"}


@router.post("/files/{owned_file_id}/{lang}/swap", dependencies=[Depends(require_token)])
async def api_swap(owned_file_id: int, lang: LangPath, body: SwapRequest) -> dict[str, Any]:
    config = await asyncio.to_thread(effective_config)
    with _review_errors():
        return await swap(config, owned_file_id, lang, body.candidate)


@router.post("/files/{owned_file_id}/{lang}/revert", dependencies=[Depends(require_token)])
async def api_revert(owned_file_id: int, lang: LangPath) -> dict[str, Any]:
    config = await asyncio.to_thread(effective_config)
    with _review_errors():
        await revert(config, owned_file_id, lang)
    return {"status": "reverted"}
