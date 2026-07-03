"""Shared error types + secret redaction.

``NotConfiguredError`` marks "a provider/credential is missing" so callers
(scheduler jobs, API routes) can skip/503 without swallowing real bugs raised as
plain ``ValueError``. ``InvalidTransitionError`` guards the candidate state
machine. ``redact()`` strips credentials from text that came near an exception
(httpx error messages include full request URLs, query string included) before
it is logged, stored in ``job_run.stats``, notified, or rendered.
"""

from __future__ import annotations

import re

_QUERY_SECRET_RE = re.compile(r"(?i)\b(api_?key|apikey|token|password|secret)=[^&\s'\"]+")
_TELEGRAM_BOT_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")


class NotConfiguredError(ValueError):
    """A required provider/credential is not configured; the operation is skipped."""


class InvalidTransitionError(ValueError):
    """The requested candidate status change is not allowed from its current state."""


def redact(text: str) -> str:
    """Strip API keys/tokens (query params, Telegram bot paths) from ``text``."""

    text = _QUERY_SECRET_RE.sub(lambda m: m.group(0).split("=")[0] + "=REDACTED", text)
    return _TELEGRAM_BOT_RE.sub("/botREDACTED", text)


def redact_exc(exc: BaseException) -> str:
    """``str(exc)`` with secrets stripped — the only safe way to persist/display one."""

    return redact(str(exc))
