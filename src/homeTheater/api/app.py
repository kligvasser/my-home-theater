"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import get_config
from ..db import init_db
from ..logging_setup import ensure_logging_configured, get_logger
from . import candidates, catalog, health, insights, settings, status, subtitles, web, webhooks
from .templates import STATIC_DIR

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = get_config()
    # No-op when the CLI already configured logging; its LOG_LEVEL/LOG_JSON win.
    ensure_logging_configured()
    # Dev convenience: ensure tables exist. Production relies on Alembic.
    init_db()
    log.info(
        "app.startup",
        version=__version__,
        dry_run=cfg.features.dry_run,
        auto_approve=cfg.features.auto_approve,
        scheduler=cfg.schedule.enabled,
    )

    scheduler = None
    if cfg.schedule.enabled:
        from ..scheduler import build_scheduler

        scheduler = build_scheduler(cfg)
        scheduler.start()
        app.state.scheduler = scheduler

    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)
    log.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="my-home-theater",
        version=__version__,
        summary="Personal movie & TV library automation.",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(health.router)
    app.include_router(catalog.router)
    app.include_router(candidates.router)
    app.include_router(subtitles.router)
    app.include_router(webhooks.router)
    app.include_router(status.router)
    app.include_router(insights.router)
    app.include_router(settings.router)
    app.include_router(web.router)
    return app


app = create_app()
