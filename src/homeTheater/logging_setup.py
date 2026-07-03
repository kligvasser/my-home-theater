"""Structured, run-scoped logging via structlog.

Call :func:`configure_logging` once at startup. Use :func:`bind_run` to attach a
``run_id`` (and any other context) to every log line inside a job, so a scan or
discovery run is greppable end to end.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Configure stdlib + structlog. ``json_logs`` for container/prod, pretty for dev."""

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

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
