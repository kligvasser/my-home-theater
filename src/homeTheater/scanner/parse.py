"""Filename parsing (guessit) + subtitle sidecar detection.

Pure functions, no I/O, so they're cheap to unit-test against edge cases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from guessit import guessit

from ..db.models import TitleKind

# Video containers we treat as media. Lowercased, with leading dot.
MEDIA_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg"}
)
SUBTITLE_EXTENSIONS = frozenset({".srt", ".sub", ".ass", ".ssa", ".vtt", ".smi"})

# Fallback language code when a sidecar has no language tag in its name.
UNKNOWN_LANG = "und"


@dataclass(frozen=True, slots=True)
class ParsedMedia:
    title: str
    kind: TitleKind
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    codec: str | None = None
    container: str | None = None


def is_media_file(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS


def is_subtitle_file(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in SUBTITLE_EXTENSIONS


def _first(value: object) -> object:
    """guessit returns a list for multi-value fields (e.g. multi-episode)."""

    if isinstance(value, list):
        return value[0] if value else None
    return value


def parse_media(name: str, kind_hint: TitleKind | None = None) -> ParsedMedia:
    """Parse a media filename into structured fields.

    ``kind_hint`` (from which NAS root the file lives under) is authoritative for
    movie vs. series, since the library is organized into ``Movies`` / ``TV Shows``
    and guessit defaults ambiguous names to "movie". When no hint is given we fall
    back to guessit's own ``type``. Season/episode still come from guessit.
    """

    info = guessit(name)
    if kind_hint is not None:
        kind = kind_hint
    else:
        kind = TitleKind.series if info.get("type") == "episode" else TitleKind.movie

    title = str(info.get("title") or os.path.splitext(name)[0]).strip()

    year = info.get("year")
    season = _first(info.get("season")) if kind is TitleKind.series else None
    episode = _first(info.get("episode")) if kind is TitleKind.series else None
    container = info.get("container")

    return ParsedMedia(
        title=title,
        kind=kind,
        year=int(year) if isinstance(year, int) else None,
        season=int(season) if isinstance(season, int) else None,
        episode=int(episode) if isinstance(episode, int) else None,
        resolution=info.get("screen_size"),
        codec=info.get("video_codec"),
        container=str(container) if container else None,
    )


def subtitle_lang_for(media_name: str, sub_name: str) -> str | None:
    """Return the language of ``sub_name`` if it's a sidecar of ``media_name``.

    Matches by shared stem, then reads the tag between the media stem and the
    subtitle extension: ``Movie (2020) 1080p.he.srt`` -> ``he``. A bare
    ``Movie (2020) 1080p.srt`` -> :data:`UNKNOWN_LANG`. Returns ``None`` if the
    subtitle does not belong to the media file.
    """

    media_stem = os.path.splitext(media_name)[0]
    sub_stem, sub_ext = os.path.splitext(sub_name)
    if sub_ext.lower() not in SUBTITLE_EXTENSIONS:
        return None
    if sub_stem == media_stem:
        return UNKNOWN_LANG
    if not sub_stem.startswith(media_stem):
        return None

    remainder = sub_stem[len(media_stem) :].strip(". ")
    if not remainder:
        return UNKNOWN_LANG
    # Take the last dotted token as the language tag, e.g. "en.forced" -> "forced"
    # is not a language, so prefer a 2-3 letter alpha token.
    tokens = [t for t in remainder.split(".") if t]
    for token in tokens:
        if 2 <= len(token) <= 3 and token.isalpha():
            return token.lower()
    return UNKNOWN_LANG
