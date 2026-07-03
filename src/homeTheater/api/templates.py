"""Jinja2 templating setup shared by the HTML routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from ..dashboard import human_size

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["human_size"] = human_size
