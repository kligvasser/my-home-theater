"""NAS trash: what's restorable, restore and purge. All token-gated: it lists library
paths, and restore and purge write to the NAS."""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..cleanup import _on_nas_thread
from ..config import get_config
from ..errors import JobBusyError, NotConfiguredError, redact_exc
from ..trash import (
    TrashConflictError,
    TrashError,
    TrashUnavailableError,
    list_trash,
    purge,
    restore,
)
from .auth import require_token

router = APIRouter(prefix="/api/trash", tags=["trash"])

_RESTORE_TIMEOUT_SECONDS = 60.0
_PURGE_TIMEOUT_SECONDS = 300.0


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=404, detail="trash item not found")
    if isinstance(exc, NotConfiguredError | TrashUnavailableError | TimeoutError):
        detail = redact_exc(exc) if not isinstance(exc, TimeoutError) else "NAS didn't answer"
        return HTTPException(status_code=503, detail=detail)
    if isinstance(exc, JobBusyError | TrashConflictError):
        return HTTPException(status_code=409, detail=redact_exc(exc))
    return HTTPException(status_code=400, detail=redact_exc(exc))


_ERRORS = (ValueError, NotConfiguredError, TimeoutError, JobBusyError, TrashError)


@router.get("", dependencies=[Depends(require_token)])
def api_trash() -> dict[str, Any]:
    cfg = get_config()
    return {
        "retention_days": cfg.trash.retention_days,
        "items": [asdict(entry) for entry in list_trash(cfg)],
    }


@router.post("/{item_id}/restore", dependencies=[Depends(require_token)])
async def api_restore(item_id: int) -> dict[str, Any]:
    """Put a file back. 409 if something is already there: nothing is overwritten."""

    try:
        path = await _on_nas_thread(_RESTORE_TIMEOUT_SECONDS, restore, get_config(), item_id)
    except _ERRORS as exc:
        raise _http_error(exc) from exc
    return {"restored": path}


@router.post("/purge", dependencies=[Depends(require_token)])
async def api_purge() -> dict[str, Any]:
    """Delete what's past the retention period now. Nothing younger is touched."""

    cancel = threading.Event()

    def run() -> dict[str, Any]:
        return purge(get_config(), cancel=cancel).as_dict()

    try:
        return await _on_nas_thread(_PURGE_TIMEOUT_SECONDS, run)
    except _ERRORS as exc:
        cancel.set()
        raise _http_error(exc) from exc
