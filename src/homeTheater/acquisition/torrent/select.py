"""Query building + release selection for the torrent backend.

Given a title and a bag of search hits from all enabled sources, pick the one
release to grab: drop under-seeded and wrong-resolution releases, then rank by
resolution preference (the operator's ``allowed_resolutions`` order) and seeders.
Season-scoped grabs additionally verify the release actually IS that season —
a "Title Season 3" search happily returns other seasons' packs.
"""

from __future__ import annotations

import re

from guessit import guessit

from ...db.models import TitleKind
from .base import TorrentRelease

# Release names that are executables in disguise ("Movie 2026 1080p ... .exe",
# often with padding spaces before the extension). Never grab these.
_EXECUTABLE_NAME = re.compile(
    r"\.\s*(exe|scr|bat|cmd|com|msi|pif|vbs|jar|apk|dmg|lnk)\s*$", re.IGNORECASE
)

# Low-quality / pre-retail sources — never HD, often fakes. Matched as delimited
# release tags (surrounded by space/dot/dash/underscore/bracket) so short tokens
# like "ts"/"tc" don't fire inside real words ("Guts", "Match").
_LOW_QUALITY = re.compile(
    r"(?i)(?:^|[\s.\-_\[(])"
    r"(cam|camrip|hdcam|ts|telesync|hdts|hdtc|tc|telecine|scr|screener|dvdscr"
    r"|r5|workprint|wp|pdvd|predvd|dvdscreener)"
    r"(?:$|[\s.\-_\])])"
)

# SD / legacy containers & codecs: a modern HD release is mkv/mp4 + x264/x265,
# never avi/xvid/divx. Their presence marks an old rip or a fake.
_SD_CONTAINER = re.compile(r"(?i)(?:^|[\s.\-_\[(])(avi|xvid|divx)(?:$|[\s.\-_\])])")

# Resolutions we consider HD.
_HD_RESOLUTIONS = frozenset({"720p", "1080p", "1440p", "2160p", "4320p"})


def _title_tokens(name: str) -> list[str]:
    """Lower-case alphanumeric tokens of a title (for movie title matching)."""

    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _release_is_title(release_name: str, expected_title: str) -> bool:
    """Does a release actually belong to ``expected_title``? guessit's parsed
    title must START with the movie's title tokens — so "Digger" matches
    "Digger 2026 1080p..." but not "OnlyTarts ... Gold Digger ..." (title
    "OnlyTarts") or "Moonshiners S16E04 Digger Knows Best" (title "Moonshiners")."""

    want = _title_tokens(expected_title)
    if not want:
        return True
    parsed = guessit(release_name).get("title")
    if not parsed:
        return False
    got = _title_tokens(str(parsed))
    return got[: len(want)] == want


def _release_year(release_name: str) -> int | None:
    y = guessit(release_name).get("year")
    return int(y) if isinstance(y, int) else None


def build_query(title: str, year: int | None, kind: TitleKind, season: int | None = None) -> str:
    """A search string broad enough to get hits but specific enough to be relevant.

    Movies add the year (cheap disambiguation); series don't, since a season/pack
    release rarely carries the first-air year in its name. Season-scoped
    candidates target that season's pack ("Title S03").

    Apostrophes are stripped: scene releases drop them, so the TMDb title
    "Widow's Bay" must be searched as "Widows Bay" or the indexer returns nothing.
    """

    # Remove apostrophe variants (straight, curly, backtick) — "Widow's" -> "Widows".
    title = re.sub(r"['‘’`]", "", title).strip()
    if kind is TitleKind.movie and year:
        return f"{title} {year}"
    if kind is TitleKind.series and season is not None:
        return f"{title} S{season:02d}"
    return title


def detect_resolution(release_name: str) -> str | None:
    """Normalised resolution parsed from a release name (e.g. '1080p', '2160p')."""

    res = guessit(release_name).get("screen_size")
    if not res:
        return None
    res = str(res).lower()
    return "2160p" if res in {"4k", "uhd"} else res


def parse_season_episode(release_name: str) -> tuple[list[int], list[int]]:
    """(seasons, episodes) guessit reads from a release name.

    Either list may be empty (a movie, or a season pack with no episode number);
    multi-season packs ("S01-S03") and multi-episode files ("S03E01E02") yield
    several values.
    """

    info = guessit(release_name)

    def as_list(value: object) -> list[int]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        return [int(str(v)) for v in items]

    return as_list(info.get("season")), as_list(info.get("episode"))


def select_release(
    releases: list[TorrentRelease],
    *,
    allowed_resolutions: list[str],
    min_seeders: int,
    season: int | None = None,
    episode: int | None = None,
    banned: frozenset[str] | None = None,
    require_hd: bool = True,
    movie: bool = False,
    expected_title: str | None = None,
    expected_year: int | None = None,
) -> TorrentRelease | None:
    """Best downloadable release, or ``None`` if nothing qualifies.

    A release is dropped when it has no usable magnet, too few seeders, an
    executable-styled name, or a *detected* resolution outside
    ``allowed_resolutions``. Releases whose resolution can't be parsed are kept
    but ranked below explicit matches.

    With ``season`` set, only releases that parse to exactly that season
    qualify (multi-season packs are excluded — they'd re-download seasons you
    own). ``episode=None`` then means "the season pack" (episode-numbered
    releases are dropped); ``episode=e`` means a release containing episode e.

    ``banned`` infohashes (previously quarantined as malware/junk) are never
    picked again — their listing names look clean, only their content told.

    ``movie`` rejects releases that parse as a TV episode/season — a movie search
    ("Digger 2026") otherwise matches an episode whose *title* contains the word
    ("Moonshiners S16E04 Digger Knows Best").

    ``require_hd`` (default) rejects cam/telesync/telecine/screener sources, SD
    containers (avi/xvid/divx), and releases with no detectable HD resolution —
    so a fake "WEB-DL [Foreign Dub]" with no resolution, or a TC/CAM rip, is
    never grabbed. Turn it off to accept lower-quality or unlabeled releases.
    """

    allowed = [r.lower() for r in allowed_resolutions] if allowed_resolutions else []
    scored: list[tuple[int, int, TorrentRelease]] = []
    for rel in releases:
        if rel.magnet_uri() is None or rel.seeders < min_seeders:
            continue
        if _EXECUTABLE_NAME.search(rel.title):
            continue  # malware bait, whatever its seeders say
        if banned and rel.infohash and rel.infohash.lower() in banned:
            continue  # quarantined before; the same fake resurfaces in searches
        if require_hd and (_LOW_QUALITY.search(rel.title) or _SD_CONTAINER.search(rel.title)):
            continue  # cam/ts/tc/screener or avi/xvid — never real HD
        if movie:
            seasons, episodes = parse_season_episode(rel.title)
            if seasons or episodes:
                continue  # a TV episode/season, not the movie we asked for
            if expected_title and not _release_is_title(rel.title, expected_title):
                continue  # the movie name appears, but it's a different release
            if expected_year is not None:
                ry = _release_year(rel.title)
                if ry is not None and abs(ry - expected_year) > 1:
                    continue  # a different film sharing the title (Digger 2020 vs 2026)
        if season is not None:
            seasons, episodes = parse_season_episode(rel.title)
            if seasons != [season]:
                continue
            if episode is None:
                if episodes:  # single episode, not the pack we want
                    continue
            elif episode not in episodes:
                continue
        res = detect_resolution(rel.title)
        if res is None:
            # No parseable resolution: only a fake/old rip hides it. With
            # require_hd we skip it rather than gamble (this is exactly the
            # "Bad Apples WEB-DL [Ukr Dub]" shape).
            if require_hd:
                continue
        else:
            if res not in _HD_RESOLUTIONS and require_hd:
                continue
            if allowed and res not in allowed:
                continue
        # Lower rank = better: preferred resolutions first (by the operator's
        # ordering), unknown-resolution last; then more seeders wins.
        res_rank = allowed.index(res) if (allowed and res in allowed) else len(allowed)
        scored.append((res_rank, -rel.seeders, rel))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][2]
