"""Persistent, TTL'd cache for external provider responses.

Keyed by ``(provider, cache_key)``. A ``ttl_days`` of 0 disables reads (always a
miss) but still writes, which is handy for forced refreshes.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any

from sqlalchemy import select

from ..db.base import utcnow
from ..db.models import MetadataCache
from ..db.session import session_scope


def cache_get(provider: str, key: str, ttl_days: int) -> dict[str, Any] | None:
    """Return the cached payload if present and fresher than ``ttl_days``."""

    if ttl_days <= 0:
        return None
    with session_scope() as session:
        row = session.scalar(
            select(MetadataCache).where(
                MetadataCache.provider == provider, MetadataCache.cache_key == key
            )
        )
        if row is None or row.payload is None:
            return None
        fetched = row.fetched_at
        # SQLite may return naive datetimes; treat them as UTC.
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        if utcnow() - fetched > timedelta(days=ttl_days):
            return None
        return dict(row.payload)


def cache_set(provider: str, key: str, payload: dict[str, Any]) -> None:
    """Upsert a cached payload, stamping the fetch time."""

    with session_scope() as session:
        row = session.scalar(
            select(MetadataCache).where(
                MetadataCache.provider == provider, MetadataCache.cache_key == key
            )
        )
        if row is None:
            row = MetadataCache(provider=provider, cache_key=key)
            session.add(row)
        row.payload = payload
        row.fetched_at = utcnow()
