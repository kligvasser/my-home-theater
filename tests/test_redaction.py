"""S18: secrets stay out of everything an exception or log line can reach."""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest
import structlog

from homeTheater.errors import redact, redact_exc
from homeTheater.logging_setup import configure_logging, redact_event


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("GET https://x/y?api_key=SEKRET&page=1", "SEKRET"),
        ("GET https://x/y?apikey=SEKRET", "SEKRET"),
        ("access_token=SEKRET", "SEKRET"),
        ("refresh_token=SEKRET&x=1", "SEKRET"),
        ("client_secret=SEKRET", "SEKRET"),
        ("X_Session_Key=SEKRET", "SEKRET"),
        ("smb_password=SEKRET", "SEKRET"),
        ("http://user:SEKRET@localhost:9091/transmission/rpc failed", "SEKRET"),
        ("https://SEKRET@example.com/", "SEKRET"),
        ("POST http://kodi:SEKRET@kodi.local:8080/jsonrpc failed", "SEKRET"),
        ("POST http://kodi:p@SEKRET@kodi.local:8080/jsonrpc failed", "SEKRET"),
        ("smb://kodi:SEKRET@NAS/Share/Movies/a.mkv", "SEKRET"),
        ("KODI_PASS=SEKRET", "SEKRET"),
        ("https://dl.opensubtitles.org/en/download/sid-SEKRET/file/1", "SEKRET"),
        ("https://x/bot123456:SEKRET/sendMessage", "SEKRET"),
        ("Authorization: Bearer SEKRET.abc-1", "SEKRET"),
        ("{'password': 'SEKRET', 'username': 'u'}", "SEKRET"),
        ('{"api_key": "SEKRET"}', "SEKRET"),
    ],
)
def test_redact_removes_the_secret(text: str, secret: str) -> None:
    assert secret not in redact(text)


def test_redact_leaves_the_diagnostics() -> None:
    text = "Client error '401' for url 'https://api.x.org/3/movie?api_key=abc&page=2'"
    out = redact(text)
    assert out == "Client error '401' for url 'https://api.x.org/3/movie?api_key=REDACTED&page=2'"
    assert redact("Bearer tokens expired for /Volumes/x.mkv") != ""
    assert redact("https://example.com/path") == "https://example.com/path"


def test_redact_exc_names_the_type_when_the_message_is_empty() -> None:
    """str(ReadTimeout("")) is empty: the dashboard showed a blank error."""

    assert redact_exc(httpx.ReadTimeout("")) == "ReadTimeout"
    assert redact_exc(TimeoutError()) == "TimeoutError"
    assert redact_exc(ValueError("   ")) == "ValueError"


def test_event_processor_covers_rendered_tracebacks_and_nested_values() -> None:
    try:
        raise RuntimeError("boom https://x/y?api_key=SEKRET")
    except RuntimeError:
        event = structlog.processors.format_exc_info(
            None, "error", {"event": "job.failed", "exc_info": True}
        )
    out = redact_event(
        None,
        "error",
        {
            **event,
            "url": "https://u:SEKRET@x/",
            "nested": {"list": ["token=SEKRET"], "exc": RuntimeError("password=SEKRET")},
            "api_key": "SEKRET",
            "dashboard_token": "SEKRET",
        },
    )
    assert "SEKRET" not in repr(out)
    assert "RuntimeError" in out["exception"]  # the traceback itself survives


def test_structlog_output_is_redacted_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    previous = logging.getLogger().level
    structlog.reset_defaults()
    configure_logging(level="INFO", json_logs=True)
    try:
        try:
            raise RuntimeError("401 for https://x/y?api_key=SEKRET")
        except RuntimeError:
            structlog.get_logger("t").exception("job.failed", url="https://x/?token=SEKRET")
        line = capsys.readouterr().out.strip().splitlines()[-1]
    finally:
        structlog.reset_defaults()
        logging.getLogger().setLevel(previous)
    record = json.loads(line)
    assert "SEKRET" not in line
    assert "api_key=REDACTED" in record["exception"]


def _render(logger: logging.Logger, formatter: logging.Formatter, emit: object) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        emit()  # type: ignore[operator]
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_stdlib_tracebacks_are_redacted() -> None:
    """Uvicorn logs unhandled route errors itself, bypassing structlog."""

    configure_logging(level="INFO")
    logger = logging.getLogger("redaction.test.error")

    def emit() -> None:
        try:
            raise RuntimeError("401 for https://x/y?api_key=SEKRET")
        except RuntimeError:
            logger.exception("Exception in ASGI application")

    out = _render(logger, logging.Formatter("%(message)s"), emit)
    assert "RuntimeError" in out and "SEKRET" not in out


def test_uvicorn_access_lines_lose_the_webhook_token() -> None:
    """Radarr's webhook URL carries ?token=. Uvicorn's formatter unpacks args, so
    their shape has to survive."""

    from uvicorn.logging import AccessFormatter

    configure_logging(level="INFO")
    logger = logging.getLogger("redaction.test.access")
    out = _render(
        logger,
        AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False),
        lambda: logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "10.0.0.2:1",
            "POST",
            "/api/webhooks/radarr?token=SEKRET",
            "1.1",
            200,
        ),
    )
    assert "SEKRET" not in out and "POST /api/webhooks/radarr?token=REDACTED" in out
