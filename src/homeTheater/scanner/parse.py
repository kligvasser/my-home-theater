"""Filename parsing (guessit) + subtitle sidecar detection.

Pure functions, no I/O, so they're cheap to unit-test against edge cases.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# babelfish ships with guessit; untyped, hence the ignore (like guessit itself).
from babelfish import (  # type: ignore[import-untyped]
    Language,
    LanguageReverseError,
)
from guessit import guessit

from ..db.models import TitleKind
from ..metadata.names import names_match

# Video containers we treat as media. Lowercased, with leading dot.
MEDIA_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg"}
)
SUBTITLE_EXTENSIONS = frozenset({".srt", ".sub", ".ass", ".ssa", ".vtt", ".smi"})

# Fallback language code when a sidecar has no language tag in its name.
UNKNOWN_LANG = "und"

# iw is what Hebrew was before ISO renamed it; old release tools still write it.
_LANGUAGE_ALIASES = {"iw": "he"}
# On a subtitle "hi" nearly always means hearing-impaired, not Hindi.
_HEARING_IMPAIRED_TAG = "hi"

_SEASON_DIR_RE = re.compile(r"(?i)^(season|series|s|עונה)[\s._-]*\d+$|^specials$|^extras$")
# Where a release-style folder name stops being the show's name. Only tags no
# show title ends in: "Charlotte's Web" and "Complete Unknown" are names.
_RELEASE_TAG_RE = re.compile(
    r"(?i)[\s._-]+(?:s\d{1,3}(?:e\d+)?|season[\s._-]*\d+|\d{3,4}p|4k|web[\s._-]?dl|webrip|"
    r"bluray|blu-ray|brrip|hdtv|x26[45]|h[\s.]?26[45]|hevc)(?:[\s._-].*)?$"
)
# A year is a tag in brackets or scene dots. After a plain space it may be the name:
# "Class of 2020".
_FOLDER_YEAR_RE = re.compile(r"(?:\s*[(\[]((?:19|20)\d{2})[)\]]|[._]((?:19|20)\d{2}))$")


@dataclass(frozen=True, slots=True)
class ParsedMedia:
    title: str
    kind: TitleKind
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None  # last episode of a multi-episode file
    resolution: str | None = None
    codec: str | None = None
    container: str | None = None
    show_folder: str | None = None  # the folder that named this series, as on disk


def is_media_file(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS


def is_subtitle_file(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in SUBTITLE_EXTENSIONS


def _first(value: object) -> object:
    """guessit returns a list for multi-value fields (e.g. multi-episode)."""

    if isinstance(value, list):
        return value[0] if value else None
    return value


def _last(value: object) -> object:
    if isinstance(value, list):
        return value[-1] if value else None
    return value


def series_identity(folder: str) -> tuple[str, int | None] | None:
    """``(title, year)`` a show's top-level folder stands for, or ``None``.

    Deliberately not guessit: it reads "The 100" as an episode, "9-1-1" as a title
    of "1-1", "1923" as a year, and "Dexter - Resurrection" as plain "Dexter". The
    folder is a name someone chose, so it's taken literally apart from the release
    tags and year a folder can carry. A ``Season 01`` folder names no show.
    """

    name = folder.strip()
    if not name or _SEASON_DIR_RE.match(name):
        return None
    name = _RELEASE_TAG_RE.sub("", name)
    year: int | None = None
    if (match := _FOLDER_YEAR_RE.search(name)) and match.start() > 0:
        year = int(match.group(1) or match.group(2))
        name = name[: match.start()]
    if " " not in name:  # Dotted scene style: The.White.Lotus
        name = name.replace(".", " ").replace("_", " ")
    title = name.strip(" .-_")
    return (title, year) if title else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _parsed(
    info: Mapping[str, Any],
    title: str,
    year: object,
    kind: TitleKind,
    show_folder: str | None = None,
) -> ParsedMedia:
    season = episode = episode_end = None
    if kind is TitleKind.series:
        season = _int(_first(info.get("season")))
        episode = _int(_first(info.get("episode")))
        last = _int(_last(info.get("episode")))
        episode_end = last if last is not None and last != episode else None
        # Mini-series name their episodes "Part 2", with no season of their own.
        if episode is None and (part := _int(_first(info.get("part")))) is not None:
            episode = part
            season = 1 if season is None else season
    elif (part := _int(_first(info.get("part")))) is not None:
        # "Dune Part Two" is a film of its own, not a second copy of "Dune".
        title = f"{title} Part {part}"

    container = info.get("container")
    return ParsedMedia(
        title=title,
        kind=kind,
        year=_int(year),
        season=season,
        episode=episode,
        episode_end=episode_end,
        resolution=info.get("screen_size"),
        codec=info.get("video_codec"),
        container=str(container) if container else None,
        show_folder=show_folder,
    )


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
    back to guessit's own ``type``.

    A series file below a show folder takes its title and year from that top-level
    folder alone, and only season/episode from the rest of the path. Filenames
    disagree within one show (``bkd-tk.s01e06.mkv``, ``Severance (2021) - S02E01``,
    an anime's romanized name under an English folder), and each disagreement used
    to create a separate title.
    """

    normalized = path.replace("\\", "/").strip("/")
    head, sep, rest = normalized.partition("/")
    identity = series_identity(head) if sep and kind_hint is TitleKind.series else None
    if identity is not None:
        title, year = identity
        info = dict(guessit(rest, {"type": "episode"}))
        # "Part 2" counts as an episode only of this show: "Making Of Part 2" in a
        # season folder is an extra.
        file_title = _first(info.get("title"))
        if file_title and not names_match(str(file_title), [title]):
            info.pop("part", None)
        return _parsed(info, title, year, TitleKind.series, show_folder=head)

    info = guessit(normalized)
    if kind_hint is not None:
        kind = kind_hint
    else:
        kind = TitleKind.series if info.get("type") == "episode" else TitleKind.movie

    raw_title = _first(info.get("title"))
    title = str(raw_title).strip() if raw_title else ""
    if not title:
        return None
    return _parsed(info, title, info.get("year"), kind)


def _language_code(token: str) -> str | None:
    """Normalized (2-letter when possible) code for a real ISO language token.

    Validated against babelfish so release tags like ``sdh`` or ``forced`` are
    not mistaken for languages. 3-letter codes normalize to their 2-letter form
    (``heb`` -> ``he``) so coverage counting sees one code per language.
    """

    code = _LANGUAGE_ALIASES.get(token.lower(), token.lower())
    try:
        if len(code) == 2:
            Language.fromalpha2(code)
            return code
        if len(code) == 3:
            lang = Language.fromalpha3b(code)
            return str(getattr(lang, "alpha2", None) or code)
    except (ValueError, LanguageReverseError):
        return None
    return None


def _language_named(token: str) -> str | None:
    """Code for a language written out in full: ``Hebrew`` -> ``he``."""

    if len(token) < 4:
        return None
    try:
        lang = Language.fromname(token.capitalize())
    except Exception:  # noqa: BLE001 - babelfish raises several exc types
        return None
    return str(getattr(lang, "alpha2", None) or lang.alpha3)


_TOKEN_SPLIT_RE = re.compile(r"[._\-\s]+")


def subtitle_lang_standalone(sub_name: str) -> str | None:
    """Language of a subs-folder file that carries no media stem.

    Release ``Subs/`` folders name files by language alone: ``2_English.srt``,
    ``Hebrew.srt``, ``heb.srt``. Tokens are checked as ISO codes first, then as
    full language names (babelfish). Unrecognized names -> :data:`UNKNOWN_LANG`;
    non-subtitle extensions -> ``None``.
    """

    stem, ext = os.path.splitext(sub_name)
    if ext.lower() not in SUBTITLE_EXTENSIONS:
        return None
    for token in _TOKEN_SPLIT_RE.split(stem):
        if not token or token.isdigit():
            continue
        if token.lower() == _HEARING_IMPAIRED_TAG:
            continue
        if (code := _language_code(token) or _language_named(token)) is not None:
            return code
    return UNKNOWN_LANG


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
        if token.lower() == _HEARING_IMPAIRED_TAG:
            continue
        if (code := _language_code(token) or _language_named(token)) is not None:
            return code
    return UNKNOWN_LANG
