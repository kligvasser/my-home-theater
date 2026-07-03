"""Declarative base and shared column helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base for all ORM models."""


class TimestampMixin:
    """Adds created/updated timestamps.

    The client-side ``default=utcnow`` always fires for ORM inserts so both
    columns are consistently timezone-aware (SQLite's ``CURRENT_TIMESTAMP``
    server default stores naive strings, and mixing naive and aware values
    breaks datetime comparisons). The server default remains as a safety net
    for non-ORM inserts.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
        nullable=False,
    )
