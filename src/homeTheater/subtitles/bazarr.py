"""Thin Bazarr REST client (plan §5.5).

We do not fetch or place subtitle files — Bazarr fronts OpenSubtitles/ktuvit and
does the hash/filename matching + sidecar placement. This client only *reads*
what's missing and *triggers* Bazarr to search for it.

Endpoints target the Bazarr v1 API and are all isolated here so a Bazarr change
touches one file (plan §12: isolate every external dependency behind an interface).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..db.models import TitleKind
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WantedItem:
    """A title/episode Bazarr reports as missing one or more subtitle languages."""

    kind: TitleKind
    title: str
    missing_langs: list[str]
    year: int | None = None
    radarr_id: int | None = None
    sonarr_series_id: int | None = None
    sonarr_episode_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _langs(missing: list[dict[str, Any]] | None) -> list[str]:
    out = []
    for m in missing or []:
        code = m.get("code2") or m.get("code3") or m.get("language")
        if code:
            out.append(str(code))
    return out


class BazarrClient:
    def __init__(self, base_url: str, api_key: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-API-KEY": api_key}
        self._client = client

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self._base}{path}", params=params or {}, headers=self._headers
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def system_status(self) -> dict[str, Any]:
        return await self._get("/api/system/status")

    async def wanted_movies(self) -> list[WantedItem]:
        data = await self._get("/api/movies/wanted")
        items = []
        for row in data.get("data", []):
            items.append(
                WantedItem(
                    kind=TitleKind.movie,
                    title=row.get("title", ""),
                    year=row.get("year"),
                    missing_langs=_langs(row.get("missing_subtitles")),
                    radarr_id=row.get("radarrId") or row.get("radarr_id"),
                )
            )
        return items

    async def wanted_episodes(self) -> list[WantedItem]:
        data = await self._get("/api/episodes/wanted")
        items = []
        for row in data.get("data", []):
            title = row.get("seriesTitle") or row.get("title", "")
            ep = row.get("episodeTitle")
            items.append(
                WantedItem(
                    kind=TitleKind.series,
                    title=f"{title} — {ep}" if ep else title,
                    missing_langs=_langs(row.get("missing_subtitles")),
                    sonarr_series_id=row.get("sonarrSeriesId"),
                    sonarr_episode_id=row.get("sonarrEpisodeId"),
                )
            )
        return items

    async def search_movie(self, radarr_id: int) -> None:
        """Ask Bazarr to search missing subtitles for one movie."""

        resp = await self._client.patch(
            f"{self._base}/api/movies",
            headers=self._headers,
            data={"radarrid": radarr_id, "action": "search-missing"},
        )
        resp.raise_for_status()

    async def search_episode(self, series_id: int, episode_id: int) -> None:
        resp = await self._client.patch(
            f"{self._base}/api/episodes",
            headers=self._headers,
            data={
                "seriesid": series_id,
                "episodeid": episode_id,
                "action": "search-missing",
            },
        )
        resp.raise_for_status()
