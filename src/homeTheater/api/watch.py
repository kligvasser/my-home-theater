"""The Watched page: Kodi and Trakt sync state, and archive suggestions (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from ..config import effective_config
from ..watch import KODI_STATUS_KEY, TRAKT_STATUS_KEY, archive_suggestions, load_status
from .templates import templates

router = APIRouter(include_in_schema=False)


@router.get("/watched", response_class=HTMLResponse)
def watched(request: Request) -> HTMLResponse:
    config = effective_config()
    return templates.TemplateResponse(
        request,
        "watched.html",
        {
            "kodi_enabled": config.kodi.enabled,
            "trakt_enabled": config.trakt.sync.enabled,
            "trakt_push": config.trakt.sync.push,
            "kodi_status": load_status(KODI_STATUS_KEY),
            "trakt_status": load_status(TRAKT_STATUS_KEY),
            "suggestions": archive_suggestions(),
            "active": "watched",
            "version": __version__,
        },
    )
