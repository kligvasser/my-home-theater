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
        # 3-letter codes normalize to 2-letter so coverage sees one code per lang
        ("Movie (2020) 1080p.mkv", "Movie (2020) 1080p.eng.srt", "en"),
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


def test_subtitle_lang_standalone() -> None:
    """Release-style Subs/ folder names: language-only, numbered, or coded."""

    from homeTheater.scanner.parse import subtitle_lang_standalone

    assert subtitle_lang_standalone("2_English.srt") == "en"
    assert subtitle_lang_standalone("Hebrew.srt") == "he"
    assert subtitle_lang_standalone("heb.srt") == "he"
    assert subtitle_lang_standalone("3_French.srt") == "fr"
    assert subtitle_lang_standalone("whatever.srt") == "und"
    assert subtitle_lang_standalone("notes.txt") is None


# --- show identity comes from the folder (L8) ---


@pytest.mark.parametrize(
    ("path", "title", "year", "season", "episode"),
    [
        ("The.Killing/Season 01/bkd-tk.s01e06.mkv", "The Killing", None, 1, 6),
        ("Severance/Season 02/Severance (2021) - S02E01.mkv", "Severance", None, 2, 1),
        (
            "Attack on Titan/Season 1/Shingeki no Kyojin - S01E12.mkv",
            "Attack on Titan",
            None,
            1,
            12,
        ),
        ("Happy Valley (2014)/Season 01/Happy.Valley.S01E03.mkv", "Happy Valley", 2014, 1, 3),
        ("Dexter - Resurrection/Season 01/Dexter S01E08.mkv", "Dexter - Resurrection", None, 1, 8),
        ("The 100/Season 02/The.100.S02E05.mkv", "The 100", None, 2, 5),
        ("1923/Season 01/1923.S01E02.mkv", "1923", None, 1, 2),
        ("9-1-1/Season 03/911.S03E04.mkv", "9-1-1", None, 3, 4),
        ("Shogun.S01.1080p.WEB-DL/Shogun.S01E02.mkv", "Shogun", None, 1, 2),
        ("Show/Season 2/05.mkv", "Show", None, 2, 5),
        ("שטיסל/עונה 1/Shtisel.S01E03.mkv", "שטיסל", None, 1, 3),
        ("Charlotte's Web/Season 01/cw.s01e01.mkv", "Charlotte's Web", None, 1, 1),
        ("Class of 2020/Season 01/c.s01e01.mkv", "Class of 2020", None, 1, 1),
        ("Shogun.2024.S01.1080p/Shogun.S01E02.mkv", "Shogun", 2024, 1, 2),
        ("Show [2019]/Season 01/x.s01e01.mkv", "Show", 2019, 1, 1),
    ],
)
def test_series_title_and_year_come_from_top_folder(
    path: str, title: str, year: int | None, season: int, episode: int
) -> None:
    p = parse_media(path, TitleKind.series)
    assert p is not None
    assert (p.title, p.year, p.season, p.episode) == (title, year, season, episode)
    assert p.show_folder == path.split("/")[0]


def test_season_folder_at_the_root_names_no_show() -> None:
    p = parse_media("Season 01/Breaking.Bad.S01E02.mkv", TitleKind.series)
    assert p is not None and p.title == "Breaking Bad" and p.show_folder is None


def test_hebrew_season_folder_at_the_root_names_no_show() -> None:
    p = parse_media("עונה 1/Breaking.Bad.S01E02.mkv", TitleKind.series)
    assert p is not None and p.title == "Breaking Bad"


def test_file_directly_under_root_still_parses_its_own_name() -> None:
    p = parse_media("Breaking.Bad.S03E07.mkv", TitleKind.series)
    assert p is not None and p.title == "Breaking Bad" and p.show_folder is None


def test_movies_ignore_the_folder_rule() -> None:
    p = parse_media("Some Folder/The Matrix (1999).mkv", TitleKind.movie)
    assert p is not None and p.show_folder is None


# --- L20 parse items ---


@pytest.mark.parametrize(
    ("name", "first", "last"),
    [
        ("Show/Season 01/Show.S01E01E02.mkv", 1, 2),
        ("Show/Season 01/Show.S01E01-E03.mkv", 1, 3),
        ("Show/Season 01/Show.S01E04.mkv", 4, None),
    ],
)
def test_multi_episode_file_keeps_its_range(name: str, first: int, last: int | None) -> None:
    p = parse_media(name, TitleKind.series)
    assert p is not None and (p.episode, p.episode_end) == (first, last)


def test_miniseries_part_is_its_episode() -> None:
    p = parse_media("Chernobyl/Chernobyl.Part.2.1080p.mkv", TitleKind.series)
    assert p is not None and (p.season, p.episode) == (1, 2)


def test_part_of_an_extra_is_not_an_episode() -> None:
    p = parse_media("Chernobyl/Season 01/Making Of Part 2.mkv", TitleKind.series)
    assert p is not None and p.episode is None


@pytest.mark.parametrize(
    ("path", "title"),
    [
        ("Dune Part Two (2024).mkv", "Dune Part 2"),
        ("Harry Potter and the Deathly Hallows Part 1 (2010).mkv", None),
    ],
)
def test_movie_part_stays_in_the_title(path: str, title: str | None) -> None:
    p = parse_media(path, TitleKind.movie)
    assert p is not None and "Part" in p.title
    if title:
        assert p.title == title


@pytest.mark.parametrize(
    ("media", "sub", "expected"),
    [
        ("M.mkv", "M.Hebrew.srt", "he"),
        ("M.mkv", "M.iw.srt", "he"),
        ("M.mkv", "M.English.srt", "en"),
        ("M.mkv", "M.en.hi.srt", "en"),
        ("M.mkv", "M.hi.en.srt", "en"),
        ("M.mkv", "M.hi.srt", UNKNOWN_LANG),  # hearing-impaired, of no stated language
        ("M.mkv", "M.he.forced.srt", "he"),
    ],
)
def test_subtitle_language_tokens(media: str, sub: str, expected: str) -> None:
    assert subtitle_lang_for(media, sub) == expected


def test_standalone_subtitle_treats_iw_as_hebrew() -> None:
    from homeTheater.scanner.parse import subtitle_lang_standalone

    assert subtitle_lang_standalone("2_iw.srt") == "he"
    assert subtitle_lang_standalone("Hebrew.srt") == "he"
    assert subtitle_lang_standalone("hi.en.srt") == "en"
