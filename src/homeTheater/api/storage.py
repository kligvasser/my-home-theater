"""The storage planner page. Read-only: suggestions, never deletions."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from ..config import effective_config
from ..storage import storage_plan
from .templates import templates

router = APIRouter(include_in_schema=False)


@router.get("/storage", response_class=HTMLResponse)
async def storage_page(request: Request) -> HTMLResponse:
    cfg = await asyncio.to_thread(effective_config)
    return templates.TemplateResponse(
        request,
        "storage.html",
        {"plan": await storage_plan(cfg), "active": "storage", "version": __version__},
    )
