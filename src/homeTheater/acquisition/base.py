"""LibraryAutomation interface + DTOs (plan §5.6).

The app drives Radarr/Sonarr through this narrow interface; they own release
selection, the download client, import, and renaming.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..db.models import TitleKind


@dataclass(frozen=True, slots=True)
class AddResult:
    external_id: int  # the Radarr/Sonarr internal id we track the item by
    title: str
    already_existed: bool = False


@dataclass(frozen=True, slots=True)
class ItemStatus:
    monitored: bool
    has_file: bool
    downloading: bool


@dataclass(frozen=True, slots=True)
class OwnedRef:
    external_id: int
    title: str
    tmdb_id: int | None
    tvdb_id: int | None
    has_file: bool


class LibraryAutomation(Protocol):
    kind: TitleKind

    async def add(
        self,
        external_id: int,
        *,
        quality_profile: str,
        root_folder: str | None,
        search: bool,
    ) -> AddResult:
        """Add + monitor a title (external_id = TMDb for Radarr, TVDB for Sonarr)."""
        ...

    async def status(self, item_id: int) -> ItemStatus: ...

    async def list_owned(self) -> list[OwnedRef]: ...
