"""Parser + subtitle sidecar detection (pure, no I/O)."""

from __future__ import annotations

import pytest

from homeTheater.db.models import TitleKind
from homeTheater.scanner.parse import (
    UNKNOWN_LANG,
    is_media_file,
    is_subtitle_file,
    parse_media,
    subtitle_lang_for,
)


def test_parses_movie() -> None:
    p = parse_media("The Matrix (1999) 1080p BluRay x264.mkv", TitleKind.movie)
    assert p is not None
    assert p.kind is TitleKind.movie
    assert p.title == "The Matrix"
    assert p.year == 1999
    assert p.resolution == "1080p"
    assert p.season is None and p.episode is None


def test_parses_episode() -> None:
    p = parse_media("Breaking.Bad.S03E07.720p.HDTV.x264.mkv", TitleKind.series)
    assert p is not None
    assert p.kind is TitleKind.series
    assert p.season == 3
    assert p.episode == 7


def test_show_title_comes_from_parent_dirs() -> None:
    # Common NAS layout: the show name only exists in the ancestor directories.
    p = parse_media("Breaking Bad/Season 03/S03E07.mkv", TitleKind.series)
    assert p is not None
    assert p.title == "Breaking Bad"
    assert p.season == 3
    assert p.episode == 7


def test_unc_backslash_paths_are_normalized() -> None:
    p = parse_media("Breaking Bad\\Season 03\\S03E07.mkv", TitleKind.series)
    assert p is not None
    assert p.title == "Breaking Bad"
    assert p.season == 3 and p.episode == 7


def test_unparsable_name_returns_none() -> None:
    # No title anywhere in the path: skip instead of cataloging junk ("S03E07").
    assert parse_media("S03E07.mkv", TitleKind.series) is None


def test_kind_hint_breaks_ties() -> None:
    # A bare title with no movie/episode signal falls back to the hint.
    p = parse_media("SomeRandomName.mkv", TitleKind.series)
    assert p is not None
    assert p.kind is TitleKind.series


@pytest.mark.parametrize(
    ("media", "sub", "expected"),
    [
        ("Movie (2020) 1080p.mkv", "Movie (2020) 1080p.he.srt", "he"),
        ("Movie (2020) 1080p.mkv", "Movie (2020) 1080p.srt", UNKNOWN_LANG),
        ("Movie (2020) 1080p.mkv", "Movie (2020) 1080p.en.forced.srt", "en"),
        ("Movie (2020) 1080p.mkv", "Movie (2020) 1080p.eng.srt", "eng"),
        ("Movie (2020) 1080p.mkv", "Different Movie.he.srt", None),
        ("Movie (2020) 1080p.mkv", "Movie (2020) 1080p.he.txt", None),
        # A shared prefix without a "." separator is a different release/movie.
        ("Alien.mkv", "Aliens.srt", None),
        ("Movie (2020).mkv", "Movie (2020) Part 2.srt", None),
        # "sdh" (hearing-impaired tag) must not be mistaken for a language code.
        ("Movie.mkv", "Movie.sdh.srt", UNKNOWN_LANG),
    ],
)
def test_subtitle_lang_for(media: str, sub: str, expected: str | None) -> None:
    assert subtitle_lang_for(media, sub) == expected


def test_extension_helpers() -> None:
    assert is_media_file("x.mkv") and not is_media_file("x.srt")
    assert is_subtitle_file("x.srt") and not is_subtitle_file("x.mkv")
