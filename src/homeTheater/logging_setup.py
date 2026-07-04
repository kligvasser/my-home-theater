"""Structured, run-scoped logging via structlog.

Call :func:`configure_logging` once at startup. Use :func:`bind_run` to attach a
``run_id`` (and any other context) to every log line inside a job, so a scan or
discovery run is greppable end to end.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

_configured = False


def ensure_logging_configured() -> None:
    """Configure logging from ``LOG_LEVEL``/``LOG_JSON`` env unless already done.

    The app lifespan calls this so logging works when the app module is imported
    directly (tests, ``uvicorn --reload`` workers) *without* clobbering an
    explicit :func:`configure_logging` call made by the CLI entry point.
    """

    if _configured:
        return
    configure_logging(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        json_logs=os.environ.get("LOG_JSON", "").lower() in {"1", "true", "yes"},
    )


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Configure stdlib + structlog. ``json_logs`` for container/prod, pretty for dev."""

    global _configured
    _configured = True
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # SMB/auth libraries log every packet at INFO/DEBUG — deafening during a
    # scan. Keep them at WARNING regardless of our level.
    for noisy in ("smbprotocol", "smbclient", "spnego", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_run(**context: Any) -> None:
    """Bind context (e.g. ``run_id=...``) to all logs in the current context."""

    structlog.contextvars.bind_contextvars(**context)


def clear_run() -> None:
    structlog.contextvars.clear_contextvars()
