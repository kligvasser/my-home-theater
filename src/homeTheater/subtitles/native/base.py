"""Interfaces + DTOs for native subtitle providers.

A provider is a :class:`SubtitleSource`: ``search`` returns candidate subtitles
for one owned media file + language, ``download`` fetches the chosen one as raw
``.srt`` bytes (already decompressed). The rest of the app never sees a provider's
payload — only :class:`SubtitleResult`.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Any, Protocol

from ...db.models import TitleKind


@dataclass(frozen=True, slots=True)
class SubtitleQuery:
    """Everything a provider needs to find a subtitle for one media file."""

    lang: str  # ISO-639-1 (he, en, ...)
    kind: TitleKind
    title: str
    year: int | None
    imdb_id: str | None  # movie or *series* imdb id ("tt..."), if known
    release_name: str  # media file stem, for release/name matching
    season: int | None = None  # series only
    episode: int | None = None
    moviehash: str | None = None  # OpenSubtitles hash of the file, if computable


@dataclass(frozen=True, slots=True)
class SubtitleResult:
    """One candidate subtitle. ``ref`` is the provider's opaque download handle."""

    source: str
    lang: str
    name: str  # release/description, for logging + tie-breaks
    score: float  # provider ranking hint (hash match, downloads); higher = better
    ref: dict[str, Any] = field(default_factory=dict)
    hearing_impaired: bool = False


class SubtitleSource(Protocol):
    name: str

    def supports(self, lang: str) -> bool:
        """Whether this provider can serve ``lang`` (e.g. ktuvit → Hebrew only)."""
        ...

    async def search(self, query: SubtitleQuery) -> list[SubtitleResult]: ...

    async def download(self, result: SubtitleResult) -> bytes:
        """Fetch the subtitle as decompressed ``.srt`` bytes."""
        ...


def opensubtitles_hash(path: str) -> str | None:
    """OSDb hash: 64-bit sum of file size + first & last 64 KiB (the standard
    OpenSubtitles/Kodi moviehash). Returns ``None`` for unreadable/too-small
    files, so callers fall back to id/name matching."""

    chunk = 65536
    fmt = "<q"
    step = struct.calcsize(fmt)
    try:
        size = os.path.getsize(path)
        if size < chunk * 2:
            return None
        value = size
        with open(path, "rb") as f:
            for _ in range(chunk // step):
                value = (value + struct.unpack(fmt, f.read(step))[0]) & 0xFFFFFFFFFFFFFFFF
            f.seek(size - chunk, os.SEEK_SET)
            for _ in range(chunk // step):
                value = (value + struct.unpack(fmt, f.read(step))[0]) & 0xFFFFFFFFFFFFFFFF
        return f"{value:016x}"
    except OSError:
        return None
