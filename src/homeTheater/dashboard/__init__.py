"""Read-only dashboard: queries + presentation helpers."""

from __future__ import annotations

from .queries import (
    Coverage,
    LibraryStats,
    RunRow,
    TitleRow,
    get_stats,
    list_titles,
    recent_runs,
)

__all__ = [
    "Coverage",
    "LibraryStats",
    "RunRow",
    "TitleRow",
    "get_stats",
    "human_size",
    "list_titles",
    "recent_runs",
]


def human_size(num_bytes: int | None) -> str:
    """Bytes -> a compact human string, e.g. 1536 -> '1.5 KB'."""

    if not num_bytes:
        return "0 B"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
