"""Radarr/Sonarr import webhooks -> catalog reconciliation (plan §5.7).

Configure a Webhook connection in Radarr/Sonarr pointing at these URLs with the
dashboard token, e.g. ``http://host:8000/api/webhooks/radarr?token=YOURTOKEN``.
Non-import events (Test, Grab, health) are acknowledged with ``{"handled": false}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..reconcile import parse_radarr, parse_sonarr, reconcile_import
from .auth import require_webhook_token

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/radarr", dependencies=[Depends(require_webhook_token)])
async def radarr_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    event = parse_radarr(payload)
    if event is None:
        return {"handled": False, "eventType": payload.get("eventType")}
    result = reconcile_import(event)
    return {
        "handled": True,
        "title_id": result.title_id,
        "file_created": result.file_created,
        "candidate_imported": result.candidate_imported,
    }


@router.post("/sonarr", dependencies=[Depends(require_webhook_token)])
async def sonarr_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    event = parse_sonarr(payload)
    if event is None:
        return {"handled": False, "eventType": payload.get("eventType")}
    result = reconcile_import(event)
    return {
        "handled": True,
        "title_id": result.title_id,
        "file_created": result.file_created,
        "candidate_imported": result.candidate_imported,
    }
