"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import base64
import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import AppConfig, ConfigError, get_config
from ..db import init_db
from ..errors import redact_exc
from ..logging_setup import ensure_logging_configured, get_logger
from . import (
    candidates,
    catalog,
    health,
    inbox,
    insights,
    pipeline,
    repair,
    settings,
    status,
    storage,
    subtitles,
    trash,
    watch,
    web,
    webhooks,
)
from .security import HostGuardMiddleware, SecurityHeadersMiddleware
from .templates import STATIC_DIR

log = get_logger(__name__)


def _check_auth_config(cfg: AppConfig) -> None:
    """Refuse to serve a site the owner believes is locked but can't be."""

    if cfg.secrets.dashboard_require_auth and cfg.secrets.dashboard_token is None:
        raise ConfigError(
            "DASHBOARD_REQUIRE_AUTH is set but DASHBOARD_TOKEN is not. "
            "Set a token in .env, or unset DASHBOARD_REQUIRE_AUTH."
        )


async def _recover_runs(cfg: AppConfig) -> None:
    from ..scheduler import reap_orphaned_runs

    try:
        await asyncio.to_thread(reap_orphaned_runs, cfg)
    except Exception as exc:  # stale history must not keep the dashboard down
        log.warning("app.reap_failed", error=redact_exc(exc))


async def _catch_up(scheduler: Any, cfg: AppConfig) -> None:
    from ..scheduler import catch_up

    try:
        await catch_up(scheduler, cfg, "startup")
    except Exception as exc:  # jobs still fire on their intervals
        log.warning("app.catch_up_failed", error=redact_exc(exc))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = get_config()
    # No-op when the CLI already configured logging; its LOG_LEVEL/LOG_JSON win.
    ensure_logging_configured()
    _check_auth_config(cfg)
    # Dev convenience: ensure tables exist. Production relies on Alembic.
    init_db()
    await _recover_runs(cfg)
    log.info(
        "app.startup",
        version=__version__,
        dry_run=cfg.features.dry_run,
        auto_approve=cfg.features.auto_approve,
        scheduler=cfg.schedule.enabled,
    )

    scheduler = None
    wake_watcher: asyncio.Task[None] | None = None
    if cfg.schedule.enabled:
        from ..scheduler import build_scheduler, watch_for_wake
        from ..scheduler.catchup import register_clock_jobs

        scheduler = build_scheduler(cfg)
        register_clock_jobs(scheduler, cfg)
        scheduler.start()
        app.state.scheduler = scheduler
        if cfg.schedule.catch_up:
            await _catch_up(scheduler, cfg)
            wake_watcher = asyncio.create_task(watch_for_wake(scheduler, cfg))

    yield

    if wake_watcher is not None:
        wake_watcher.cancel()
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
            or path.startswith(_GATE_EXEMPT)
            or path.startswith("/api/webhooks")
        ):
            return await call_next(request)

        if token is None:  # startup refuses this; never answer with an open site
            return JSONResponse({"detail": "DASHBOARD_TOKEN is not set."}, status_code=503)
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
    # An env var, not config: this runs at import time, before a config has to exist.
    docs = os.environ.get("HOME_THEATER_DOCS", "").lower() in {"1", "true", "yes"}
    app = FastAPI(
        title="my-home-theater",
        version=__version__,
        summary="Personal movie & TV library automation.",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    _install_site_gate(app)
    # Added last = outermost: a refused Host never reaches the site gate.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(HostGuardMiddleware)
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
    app.include_router(watch.router)
    app.include_router(trash.router)
    app.include_router(inbox.router)
    app.include_router(repair.router)
    app.include_router(storage.router)
    app.include_router(web.router)
    return app


app = create_app()
