"""Jinja2 templating setup shared by the HTML routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from ..dashboard import human_size

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _static_version() -> int:
    """Newest mtime of the static assets — cache-busts CSS/JS on every deploy
    so style changes don't require users to hard-refresh."""

    try:
        return int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file()))
    except (OSError, ValueError):
        return 0


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["human_size"] = human_size
templates.env.globals["static_v"] = _static_version()
