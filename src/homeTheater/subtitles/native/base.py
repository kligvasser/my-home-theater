"""Interfaces + DTOs for native subtitle providers.

A provider is a :class:`SubtitleSource`: ``search`` returns candidate subtitles
for one owned media file + language, ``download`` fetches the chosen one as raw
bytes (already decompressed). The rest of the app never sees a provider's payload
— only :class:`SubtitleResult`.

Empty search results mean "the provider answered: nothing". A provider that could
not answer must raise (see :class:`SubtitleSourceError`), never return ``[]``.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ...db.models import TitleKind


class SubtitleSourceError(RuntimeError):
    """A provider failed (outage, bad response); says nothing about availability."""


class SourceAuthError(SubtitleSourceError):
    """Credentials rejected: pointless to call this provider again this sweep."""


class SourceQuotaError(SubtitleSourceError):
    """Quota or rate limit hit: pointless to call this provider again this sweep."""


class InvalidSubtitleError(ValueError):
    """The payload is not a usable subtitle (HTML, binary, truncated, oversized)."""


def raise_for_http(
    resp: httpx.Response, source: str, *, quota_codes: tuple[int, ...] = (429,)
) -> None:
    """Map a non-2xx response onto the provider error types."""

    code = resp.status_code
    if resp.is_success:
        return
    if code in (401, 403):
        raise SourceAuthError(f"{source}: HTTP {code}")
    if code in quota_codes:
        raise SourceQuotaError(f"{source}: HTTP {code}")
    raise SubtitleSourceError(f"{source}: HTTP {code}")


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
    filesize: int | None = None  # media file size in bytes (for .org hash matching)

    @property
    def searchable(self) -> bool:
        """False for a series file with no season/episode: any hit would be a guess."""

        if self.kind is not TitleKind.series:
            return True
        return self.season is not None and self.episode is not None


@dataclass(frozen=True, slots=True)
class SubtitleResult:
    """One candidate subtitle. ``ref`` is the provider's opaque download handle."""

    source: str
    lang: str
    name: str  # release/description, for logging + tie-breaks
    score: float  # provider ranking hint (hash match, downloads); higher = better
    ref: dict[str, Any] = field(default_factory=dict)
    hearing_impaired: bool = False
    hash_match: bool = False  # matched the file's moviehash, not just the title
    season: int | None = None  # carried for picking the member of a season-pack zip
    episode: int | None = None


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
    except (OSError, struct.error):  # struct.error: file shrank mid-read
        return None
