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

# ``anything_token=…`` in a query string or message: api_key, apikey, access_token,
# client_secret, session_key, password…
_QUERY_SECRET_RE = re.compile(r"(?i)\b(\w*(?:token|secret|key|pass(?:word)?))=[^&\s'\"]+")
# The same names as a repr'd dict or JSON body: {'password': 'x'}, "token": "x".
_MAPPING_SECRET_RE = re.compile(
    r"""(?i)(['"]\w*(?:token|secret|key|password)['"]\s*:\s*['"])[^'"]+"""
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
# Up to the last @ before the path: a password can hold an @ of its own.
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s'\"]+@")
# opensubtitles.org puts the session id in the path of its download links.
_SID_PATH_RE = re.compile(r"/sid-[^/\s'\"?&]+")
_TELEGRAM_BOT_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")


class NotConfiguredError(ValueError):
    """A required provider/credential is not configured; the operation is skipped."""


class TransientProviderError(RuntimeError):
    """A metadata provider failed transiently (quota/outage), not definitively.
    Callers must NOT treat the title as "enrichment attempted" — so its retry
    isn't suppressed for ``cache_days`` — and should retry it next run."""


class JobBusyError(RuntimeError):
    """Another process already holds this job's lock (see :mod:`homeTheater.locks`)."""


class InvalidTransitionError(ValueError):
    """The requested candidate status change is not allowed from its current state."""


class JobDeadlineExceededError(RuntimeError):
    """A scheduled job ran past its wall-clock deadline and was cancelled.

    Deliberately not a :class:`TimeoutError`: ``OSError(ETIMEDOUT)`` from the NAS
    *is* one, and that is an ordinary job failure, not this job being cut off."""


def redact(text: str) -> str:
    """Strip credentials from ``text``: secret-named params and mapping values,
    bearer tokens, URL userinfo, session-id path segments and Telegram bot paths."""

    text = _URL_USERINFO_RE.sub(r"\1REDACTED@", text)
    text = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + "=REDACTED", text)
    text = _MAPPING_SECRET_RE.sub(lambda m: m.group(1) + "REDACTED", text)
    text = _BEARER_RE.sub("Bearer REDACTED", text)
    text = _SID_PATH_RE.sub("/sid-REDACTED", text)
    return _TELEGRAM_BOT_RE.sub("/botREDACTED", text)


def redact_exc(exc: BaseException) -> str:
    """``str(exc)`` with secrets stripped — the only safe way to persist/display one.

    Falls back to the type name: ``str(httpx.ReadTimeout(""))`` is empty, which
    would otherwise reach the dashboard as a blank error."""

    return redact(str(exc)).strip() or type(exc).__name__
