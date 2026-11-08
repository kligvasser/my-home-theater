"""Structured, run-scoped logging via structlog.

Call :func:`configure_logging` once at startup. Use :func:`bind_run` to attach a
``run_id`` (and any other context) to every log line inside a job, so a scan or
discovery run is greppable end to end.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from collections.abc import MutableMapping
from typing import Any

import structlog

from .errors import redact, redact_exc

_configured = False
_record_factory_installed = False

# An event field whose NAME says secret is dropped whatever its value looks like.
_SECRET_FIELD_RE = re.compile(r"(?i)(token|secret|password|api_?key)$")


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, BaseException):
        return redact_exc(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    return value


def redact_event(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor; runs after ``format_exc_info`` so the rendered
    traceback, whose last line is the exception message, is covered too."""

    for key, value in event_dict.items():
        if _SECRET_FIELD_RE.search(key) and value is not None:
            event_dict[key] = "REDACTED"
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


def _install_record_redaction() -> None:
    """Redact stdlib log records as they are created.

    A record factory rather than a filter because uvicorn installs its own
    handlers and loggers: this is the one hook all of them pass through. It covers
    "Exception in ASGI application" tracebacks and access-log lines, where a
    webhook's ``?token=`` would otherwise be written out. ``args`` keeps its shape
    because uvicorn's access formatter unpacks it.
    """

    global _record_factory_installed
    if _record_factory_installed:
        return
    _record_factory_installed = True
    make_record = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = make_record(*args, **kwargs)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(a) for a in record.args)
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return record

    logging.setLogRecordFactory(factory)


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
    _install_record_redaction()
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
        redact_event,
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
