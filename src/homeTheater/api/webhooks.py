"""Radarr/Sonarr import webhooks -> catalog reconciliation (plan §5.7).

Configure a Webhook connection in Radarr/Sonarr pointing at these URLs with the
webhook token, e.g. ``http://host:8000/api/webhooks/radarr?token=YOURTOKEN``.
Non-import events (Test, Grab, health) are acknowledged with ``{"handled": false}``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from json import JSONDecodeError
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..reconcile import ImportEvent, parse_radarr, parse_sonarr, reconcile_import
from .auth import require_webhook_token

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


async def _read_payload(request: Request) -> dict[str, Any]:
    """Parse the webhook body, rejecting malformed/non-object JSON with a 400."""

    try:
        payload = await request.json()
    except (JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object."
        )
    return payload


async def _handle(
    request: Request, parse: Callable[[dict[str, Any]], ImportEvent | None]
) -> dict[str, Any]:
    payload = await _read_payload(request)
    event = parse(payload)
    if event is None:
        return {"handled": False, "eventType": payload.get("eventType")}
    # reconcile_import is synchronous (SQLAlchemy); keep it off the event loop.
    result = await asyncio.to_thread(reconcile_import, event)
    return {
        "handled": True,
        "title_id": result.title_id,
        "file_created": result.file_created,
        "candidate_imported": result.candidate_imported,
    }


@router.post("/radarr", dependencies=[Depends(require_webhook_token)])
async def radarr_webhook(request: Request) -> dict[str, Any]:
    return await _handle(request, parse_radarr)


@router.post("/sonarr", dependencies=[Depends(require_webhook_token)])
async def sonarr_webhook(request: Request) -> dict[str, Any]:
    return await _handle(request, parse_sonarr)
