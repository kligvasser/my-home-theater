"""Shared async HTTP helper with light retry/backoff for provider clients."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    *,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> dict[str, Any]:
    """GET ``url`` and return parsed JSON, retrying transient errors with backoff.

    Raises :class:`httpx.HTTPStatusError` on non-retryable 4xx (e.g. bad key).
    """

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(url, params=params)
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
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            await asyncio.sleep(backoff_base * (2**attempt))
    assert last_exc is not None
    raise last_exc
