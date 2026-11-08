"""Shared async HTTP helper with light retry/backoff for provider clients."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx

from ..errors import redact_exc
from ..logging_setup import get_logger

log = get_logger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Query params that carry a provider key: never left on an exception's URL.
_SECRET_PARAMS = ("api_key", "apikey")


def _clean_request(request: httpx.Request) -> httpx.Request:
    url = request.url
    for name in _SECRET_PARAMS:
        url = url.copy_remove_param(name)
    return httpx.Request(request.method, url)


def _status_error(resp: httpx.Response) -> httpx.HTTPStatusError:
    """``raise_for_status`` without the API key: the stock exception embeds the
    full URL in its message and on ``.request``, so it ends up in tracebacks."""

    request = _clean_request(resp.request)
    clean = httpx.Response(resp.status_code, content=resp.content, request=request)
    message = f"HTTP {resp.status_code} for {request.url}"
    return httpx.HTTPStatusError(message, request=request, response=clean)


def _transport_error(exc: httpx.TransportError) -> httpx.TransportError:
    return type(exc)(redact_exc(exc), request=_clean_request(exc.request))


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> dict[str, Any]:
    """GET ``url`` and return parsed JSON, retrying transient errors with backoff.

    Raises :class:`httpx.HTTPStatusError` on non-retryable 4xx (e.g. bad key), or
    the last failure once retries run out. Both are rebuilt with secrets removed
    and raised outside the ``except``, so the original can't surface as a cause.
    """

    last_exc: httpx.TransportError | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code in RETRY_STATUSES and attempt < max_retries:
                delay = backoff_base * (2**attempt)
                # Rate-limit responses say how long to wait; honor it (capped).
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    with contextlib.suppress(ValueError):
                        delay = min(float(retry_after), 30.0)
                log.warning("http.retry", url=url, status=resp.status_code, delay=delay)
                await asyncio.sleep(delay)
                continue
            if not resp.is_success:
                break
            data: dict[str, Any] = resp.json()
            return data
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            await asyncio.sleep(backoff_base * (2**attempt))
    if last_exc is not None:
        raise _transport_error(last_exc)
    raise _status_error(resp)
