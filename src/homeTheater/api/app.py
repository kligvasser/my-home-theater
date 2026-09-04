"""FastAPI application factory."""

from __future__ import annotations

import base64
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import get_config
from ..db import init_db
from ..logging_setup import ensure_logging_configured, get_logger
from . import (
    candidates,
    catalog,
    health,
    insights,
    pipeline,
    settings,
    status,
    subtitles,
    web,
    webhooks,
)
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


# Paths reachable without auth even when the whole site is locked down: static
# assets and the health probe (for uptime monitors / container healthchecks).
_GATE_EXEMPT = ("/static/", "/health", "/ready", "/favicon.ico")


def _install_site_gate(app: FastAPI) -> None:
    """Optionally lock the ENTIRE site behind the dashboard token.

    Enabled by ``DASHBOARD_REQUIRE_AUTH=true``: every request (read pages + APIs)
    must present the token via the ``X-Auth-Token`` header (the dashboard JS) or
    HTTP Basic password (so a browser navigating to a page is prompted once).
    Off by default — LAN reads stay open, and per-endpoint ``require_token`` still
    guards mutations. Webhooks keep their own ``?token=`` auth and are exempt here.
    """

    @app.middleware("http")
    async def _gate(request: Request, call_next):  # type: ignore[no-untyped-def]
        cfg = get_config()
        token = cfg.secrets.dashboard_token
        path = request.url.path
        if (
            not cfg.secrets.dashboard_require_auth
            or token is None
            or path.startswith(_GATE_EXEMPT)
            or path.startswith("/api/webhooks")
        ):
            return await call_next(request)

        secret = token.get_secret_value()
        provided = request.headers.get("x-auth-token")
        if provided is None:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
                    provided = decoded.split(":", 1)[1] if ":" in decoded else decoded
                except Exception:
                    provided = None
        if provided is not None and hmac.compare_digest(
            provided.encode("utf-8"), secret.encode("utf-8")
        ):
            return await call_next(request)
        # Prompt browsers (Basic) while still allowing header-based API clients.
        return JSONResponse(
            {"detail": "Authentication required."},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="my-home-theater"'},
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title="my-home-theater",
        version=__version__,
        summary="Personal movie & TV library automation.",
        lifespan=lifespan,
    )
    _install_site_gate(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(health.router)
    app.include_router(catalog.router)
    app.include_router(candidates.router)
    app.include_router(subtitles.router)
    app.include_router(webhooks.router)
    app.include_router(status.router)
    app.include_router(insights.router)
    app.include_router(settings.router)
    app.include_router(pipeline.router)
    app.include_router(web.router)
    return app


app = create_app()
