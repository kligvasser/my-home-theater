"""Filename parsing (guessit) + subtitle sidecar detection.

Pure functions, no I/O, so they're cheap to unit-test against edge cases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# babelfish ships with guessit; untyped, hence the ignore (like guessit itself).
from babelfish import (  # type: ignore[import-untyped]
    Language,
    LanguageReverseError,
)
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


def parse_media(path: str, kind_hint: TitleKind | None = None) -> ParsedMedia | None:
    """Parse a media path (relative to its library root) into structured fields.

    Pass the *path*, not just the basename: for the common
    ``Show/Season 03/S03E07.mkv`` layout the show title only exists in the
    ancestor directories, and guessit reads those when given a path. Backslash
    (SMB/UNC) separators are normalized first.

    Returns ``None`` when no title can be derived (e.g. a bare ``S03E07.mkv``
    with uninformative ancestors) — callers should skip the file rather than
    catalog a junk title like "S03E07".

    ``kind_hint`` (from which NAS root the file lives under) is authoritative for
    movie vs. series, since the library is organized into ``Movies`` / ``TV Shows``
    and guessit defaults ambiguous names to "movie". When no hint is given we fall
    back to guessit's own ``type``. Season/episode still come from guessit.
    """

    info = guessit(path.replace("\\", "/"))
    if kind_hint is not None:
        kind = kind_hint
    else:
        kind = TitleKind.series if info.get("type") == "episode" else TitleKind.movie

    raw_title = _first(info.get("title"))
    title = str(raw_title).strip() if raw_title else ""
    if not title:
        return None

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


def _language_code(token: str) -> str | None:
    """Return ``token`` lowercased if it is a real ISO language code, else None.

    Validated against babelfish so release tags like ``sdh`` or ``forced`` are
    not mistaken for languages (2-letter -> ISO 639-1, 3-letter -> ISO 639-2/B).
    """

    code = token.lower()
    try:
        if len(code) == 2:
            Language.fromalpha2(code)
            return code
        if len(code) == 3:
            Language.fromalpha3b(code)
            return code
    except (ValueError, LanguageReverseError):
        return None
    return None


def subtitle_lang_for(media_name: str, sub_name: str) -> str | None:
    """Return the language of ``sub_name`` if it's a sidecar of ``media_name``.

    Matches by shared stem plus a ``.`` separator, then reads the tag between the
    media stem and the subtitle extension: ``Movie (2020) 1080p.he.srt`` -> ``he``.
    A bare ``Movie (2020) 1080p.srt`` -> :data:`UNKNOWN_LANG`. Returns ``None`` if
    the subtitle does not belong to the media file — the separator requirement
    keeps ``Aliens.srt`` from attaching to ``Alien.mkv``.
    """

    media_stem = os.path.splitext(media_name)[0]
    sub_stem, sub_ext = os.path.splitext(sub_name)
    if sub_ext.lower() not in SUBTITLE_EXTENSIONS:
        return None
    if sub_stem == media_stem:
        return UNKNOWN_LANG
    if not sub_stem.startswith(media_stem + "."):
        return None

    tokens = [t for t in sub_stem[len(media_stem) + 1 :].split(".") if t]
    for token in tokens:
        if (code := _language_code(token)) is not None:
            return code
    return UNKNOWN_LANG
