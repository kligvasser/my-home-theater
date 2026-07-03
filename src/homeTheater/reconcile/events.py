"""Parse Radarr/Sonarr webhook payloads into a normalized import event.

Radarr/Sonarr fire a webhook with ``eventType: "Download"`` when a grab is
imported. We only care about that event; everything else (Test, Grab, Rename,
delete, health) parses to ``None`` and is acknowledged without action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..db.models import TitleKind

_RES_RE = re.compile(r"(2160p|1080p|720p|576p|480p)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ImportEvent:
    kind: TitleKind
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    path: str | None = None
    resolution: str | None = None
    size_bytes: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None  # multi-episode files (S01E01E02)


def _resolution(quality: dict[str, Any] | None, media_info: dict[str, Any] | None) -> str | None:
    """Extract e.g. '1080p' from the arr quality/mediaInfo blocks."""

    if quality:
        q = quality.get("quality", {})
        res = q.get("resolution")
        if isinstance(res, int) and res:
            return f"{res}p"
        name = q.get("name") or ""
        m = _RES_RE.search(name)
        if m:
            return m.group(1).lower()
    if media_info:
        res = media_info.get("resolution") or ""
        m = _RES_RE.search(str(res))
        if m:
            return m.group(1).lower()
    return None


def _is_import(payload: dict[str, Any]) -> bool:
    return str(payload.get("eventType", "")).lower() in {"download", "import"}


def parse_radarr(payload: dict[str, Any]) -> ImportEvent | None:
    if not _is_import(payload):
        return None
    movie = payload.get("movie") or {}
    movie_file = payload.get("movieFile") or {}
    return ImportEvent(
        kind=TitleKind.movie,
        title=movie.get("title", ""),
        year=movie.get("year"),
        tmdb_id=movie.get("tmdbId"),
        imdb_id=movie.get("imdbId"),
        # File path only — folderPath is a directory and would create a bogus
        # OwnedFile row the scanner can never match.
        path=movie_file.get("path"),
        resolution=_resolution(movie_file.get("quality"), movie_file.get("mediaInfo")),
        size_bytes=movie_file.get("size"),
    )


def parse_sonarr(payload: dict[str, Any]) -> ImportEvent | None:
    if not _is_import(payload):
        return None
    series = payload.get("series") or {}
    episodes = payload.get("episodes") or []
    episode_file = payload.get("episodeFile") or {}
    first = episodes[0] if episodes else {}
    # A multi-episode file (S01E01E02) arrives as one event listing all episodes.
    numbers = sorted(
        n for e in episodes if isinstance(n := e.get("episodeNumber"), int)
    )
    return ImportEvent(
        kind=TitleKind.series,
        title=series.get("title", ""),
        year=series.get("year"),
        tmdb_id=series.get("tmdbId"),
        imdb_id=series.get("imdbId"),
        tvdb_id=series.get("tvdbId"),
        path=episode_file.get("path"),
        resolution=_resolution(episode_file.get("quality"), episode_file.get("mediaInfo")),
        size_bytes=episode_file.get("size"),
        season=first.get("seasonNumber"),
        episode=numbers[0] if numbers else first.get("episodeNumber"),
        episode_end=numbers[-1] if len(numbers) > 1 else None,
    )
