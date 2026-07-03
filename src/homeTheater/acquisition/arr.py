"""Radarr + Sonarr clients (the '*arr' v3 API is near-identical for both).

All arr endpoints live here so an API change touches one file (plan §12).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..db.models import TitleKind
from ..logging_setup import get_logger
from .base import AddResult, ItemStatus, OwnedRef

log = get_logger(__name__)


class _Arr:
    """Shared HTTP + quality-profile/root-folder resolution for Radarr/Sonarr."""

    def __init__(self, base_url: str, api_key: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}
        self._client = client

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(
            f"{self._base}{path}", params=params or {}, headers=self._headers
        )
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        resp = await self._client.post(f"{self._base}{path}", json=payload, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def quality_profile_id(self, name: str) -> int:
        for p in await self._get("/api/v3/qualityprofile"):
            if p.get("name") == name:
                return int(p["id"])
        raise ValueError(f"Quality profile {name!r} not found in {self._base}")

    async def default_root_folder(self) -> str:
        folders = await self._get("/api/v3/rootfolder")
        if not folders:
            raise ValueError(f"No root folders configured in {self._base}")
        return str(folders[0]["path"])


class RadarrClient(_Arr):
    kind = TitleKind.movie

    async def add(
        self, external_id: int, *, quality_profile: str, root_folder: str | None, search: bool
    ) -> AddResult:
        matches = await self._get("/api/v3/movie/lookup", {"term": f"tmdb:{external_id}"})
        if not matches:
            raise ValueError(f"Radarr found no movie for tmdb:{external_id}")
        payload = matches[0]
        if payload.get("id"):  # already added to Radarr
            return AddResult(int(payload["id"]), payload.get("title", ""), already_existed=True)

        payload.update(
            {
                "qualityProfileId": await self.quality_profile_id(quality_profile),
                "rootFolderPath": root_folder or await self.default_root_folder(),
                "monitored": True,
                "minimumAvailability": "released",
                "addOptions": {"searchForMovie": search},
            }
        )
        created = await self._post("/api/v3/movie", payload)
        return AddResult(int(created["id"]), created.get("title", ""))

    async def status(self, item_id: int) -> ItemStatus:
        movie = await self._get(f"/api/v3/movie/{item_id}")
        queue = await self._get("/api/v3/queue")
        records = queue.get("records", []) if isinstance(queue, dict) else queue
        downloading = any(r.get("movieId") == item_id for r in records)
        return ItemStatus(
            monitored=bool(movie.get("monitored")),
            has_file=bool(movie.get("hasFile")),
            downloading=downloading,
        )

    async def list_owned(self) -> list[OwnedRef]:
        return [
            OwnedRef(
                int(m["id"]), m.get("title", ""), m.get("tmdbId"), None, bool(m.get("hasFile"))
            )
            for m in await self._get("/api/v3/movie")
        ]


class SonarrClient(_Arr):
    kind = TitleKind.series

    async def add(
        self, external_id: int, *, quality_profile: str, root_folder: str | None, search: bool
    ) -> AddResult:
        matches = await self._get("/api/v3/series/lookup", {"term": f"tvdb:{external_id}"})
        if not matches:
            raise ValueError(f"Sonarr found no series for tvdb:{external_id}")
        payload = matches[0]
        if payload.get("id"):  # already added to Sonarr
            return AddResult(int(payload["id"]), payload.get("title", ""), already_existed=True)

        payload.update(
            {
                "qualityProfileId": await self.quality_profile_id(quality_profile),
                "rootFolderPath": root_folder or await self.default_root_folder(),
                "monitored": True,
                "seasonFolder": True,
                "addOptions": {"searchForMissingEpisodes": search},
            }
        )
        created = await self._post("/api/v3/series", payload)
        return AddResult(int(created["id"]), created.get("title", ""))

    async def status(self, item_id: int) -> ItemStatus:
        series = await self._get(f"/api/v3/series/{item_id}")
        stats = series.get("statistics", {})
        queue = await self._get("/api/v3/queue")
        records = queue.get("records", []) if isinstance(queue, dict) else queue
        downloading = any(r.get("seriesId") == item_id for r in records)
        return ItemStatus(
            monitored=bool(series.get("monitored")),
            has_file=bool(stats.get("episodeFileCount", 0)),
            downloading=downloading,
        )

    async def list_owned(self) -> list[OwnedRef]:
        out = []
        for s in await self._get("/api/v3/series"):
            stats = s.get("statistics", {})
            out.append(
                OwnedRef(
                    int(s["id"]),
                    s.get("title", ""),
                    None,
                    s.get("tvdbId"),
                    bool(stats.get("episodeFileCount", 0)),
                )
            )
        return out
