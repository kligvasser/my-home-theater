"""Acquisition: drive Radarr/Sonarr to grab approved candidates (plan §5.6)."""

from .arr import RadarrClient, SonarrClient
from .base import AddResult, ItemStatus, LibraryAutomation, OwnedRef
from .service import (
    AcquireStats,
    QueueOutcome,
    SyncStats,
    queue_approved,
    queue_candidate,
    sync_downloads,
)

__all__ = [
    "AcquireStats",
    "AddResult",
    "ItemStatus",
    "LibraryAutomation",
    "OwnedRef",
    "QueueOutcome",
    "RadarrClient",
    "SonarrClient",
    "SyncStats",
    "queue_approved",
    "queue_candidate",
    "sync_downloads",
]
