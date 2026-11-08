"""Query building + release selection for the torrent backend.

Given a title and a bag of search hits from all enabled sources, pick the one
release to grab: drop under-seeded and wrong-resolution releases, then rank by
resolution preference (the operator's ``allowed_resolutions`` order) and seeders.
Season-scoped grabs additionally verify the release actually IS that season —
a "Title Season 3" search happily returns other seasons' packs.
"""

from __future__ import annotations

import re
import unicodedata

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


# Release sizes outside these bounds (bytes) are fakes or mislabelled: a "1080p" movie
# of 90 MB, a 900 GB "episode". An unreported size passes.
_MIB = 1024**2
_GIB = 1024**3
_MOVIE_SIZE = (300 * _MIB, 150 * _GIB)
_EPISODE_SIZE = (40 * _MIB, 40 * _GIB)
_PACK_SIZE = (200 * _MIB, 500 * _GIB)

_APOSTROPHES = re.compile(r"['‘’`]")
_YEAR = re.compile(r"(?:19|20)\d\d")
_SEASON_MARKER = re.compile(
    r"(?<![a-z0-9])(?:s\d{1,3}(?:e\d{1,3})?|season|series|\d{1,2}x\d{2})(?![a-z])"
)
_DISC = re.compile(r"(?i)(?<![a-z0-9])dis[ck][^a-z0-9]*\d")
# What may follow a movie's name without making it another movie.
_EDITION_WORDS = frozenset(
    {
        "cut",
        "director",
        "directors",
        "edition",
        "extended",
        "final",
        "imax",
        "remastered",
        "theatrical",
        "uncut",
        "unrated",
    }
)
# TMDb and release names disambiguate a series with these; guessit reads the country.
_REGION_WORDS = frozenset({"us", "uk", "au", "nz", "ca"})


def _fold(text: str) -> str:
    """Case, accents and punctuation out of the way: 'Amélie' == 'Amelie',
    'Fast & Furious' == 'Fast and Furious', "Widow's" == 'Widows'."""

    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _APOSTROPHES.sub("", plain).casefold().replace("&", " and ")


def _title_tokens(name: str) -> list[str]:
    """Lower-case alphanumeric tokens of a title (for title matching)."""

    return [t for t in re.split(r"[^a-z0-9]+", _fold(name)) if t]


def _title_forms(expected_title: str) -> list[list[str]]:
    """The title's tokens, plus the bare name when it ends in a year or region
    ('Shameless (US)'), which a release usually leaves out or says some other way."""

    tokens = _title_tokens(expected_title)
    if len(tokens) > 1 and (tokens[-1] in _REGION_WORDS or _YEAR.fullmatch(tokens[-1])):
        return [tokens, tokens[:-1]]
    return [tokens]


def _text_after_title(release_name: str, expected_title: str) -> str | None:
    """The folded release name past a leading ``expected_title``, or None if it
    doesn't start with it. Separators are free, so 'Spiderman' is 'Spider-Man'."""

    folded = _fold(release_name)
    for tokens in _title_forms(expected_title):
        if tokens:
            spaced = r"[^a-z0-9]*".join(re.escape(t) for t in tokens)
            match = re.match(rf"[^a-z0-9]*{spaced}(?![a-z0-9])", folded)
            if match is not None:
                return folded[match.end() :]
    return None


def _quality_text(release_name: str, expected_title: str | None) -> str:
    """The part of a name that can carry release tags: a film called 'Cam' isn't one."""

    rest = _text_after_title(release_name, expected_title) if expected_title else None
    return rest if rest else release_name


def _movie_continues(rest: str) -> bool:
    """Does the name run on into a longer title ('Reloaded', 'Part Two', '3')?"""

    tokens = [t for t in re.split(r"[^a-z0-9]+", rest) if t]
    if not tokens or _YEAR.fullmatch(tokens[0]):
        return False
    if tokens[0].isdigit():
        return True
    info = guessit(rest)
    if info.get("part") is not None:
        return True
    extra = _title_tokens(str(info.get("title") or ""))
    return any(t not in _EDITION_WORDS for t in extra)


def _series_continues(rest: str) -> bool:
    """Is there more title before the season marker ('Origins S01' after 'Show')?"""

    head = _SEASON_MARKER.split(rest, maxsplit=1)[0]
    tokens = [t for t in re.split(r"[^a-z0-9]+", head) if t]
    return not all(t in _REGION_WORDS or _YEAR.fullmatch(t) for t in tokens)


def _guessed_title_matches(release_name: str, expected_title: str) -> bool:
    """For names that don't lead with the title ('[Group] The Matrix 1999'), going by
    guessit, which also splits names it shouldn't, so this is the second opinion."""

    parsed = guessit(release_name).get("title")
    if not parsed:
        return False
    got = "".join(_title_tokens(str(parsed)))
    return any(got == "".join(tokens) for tokens in _title_forms(expected_title))


def _release_is_title(release_name: str, expected_title: str, *, series: bool = False) -> bool:
    """Does a release actually belong to ``expected_title``? The name has to be that
    title and nothing more: "Digger" matches "Digger 2026 1080p..." but not "Gold
    Digger", "Digger Knows Best" or "Moonshiners S16E04 Digger Knows Best"."""

    if not any(_title_forms(expected_title)):
        return True  # nothing comparable (a title with no latin letters)
    rest = _text_after_title(release_name, expected_title)
    if rest is None:
        return _guessed_title_matches(release_name, expected_title)
    if series:
        return not _series_continues(rest) or _guessed_title_matches(release_name, expected_title)
    return not _movie_continues(rest)


def _release_year(release_name: str, expected_title: str | None = None) -> int | None:
    """Year of the release, read past the title: 'Blade Runner 2049' isn't from 2049."""

    y = guessit(_quality_text(release_name, expected_title)).get("year")
    return int(y) if isinstance(y, int) else None


def _sane_size(rel: TorrentRelease, bounds: tuple[int, int] | None) -> bool:
    if bounds is None or not rel.size_bytes:
        return True
    return bounds[0] <= rel.size_bytes <= bounds[1]


def search_title(title: str) -> str:
    """A title as indexers want it: scene releases drop apostrophes, so the TMDb title
    "Widow's Bay" must be searched as "Widows Bay" or the indexer returns nothing."""

    return _APOSTROPHES.sub("", title).strip()


def build_query(
    title: str,
    year: int | None,
    kind: TitleKind,
    season: int | None = None,
    *,
    episode: int | None = None,
) -> str:
    """A search string broad enough to get hits but specific enough to be relevant.

    Movies add the year (cheap disambiguation); series don't, since a season/pack
    release rarely carries the first-air year in its name. Season-scoped
    candidates target that season's pack ("Title S03"), or one ``episode``.
    """

    title = search_title(title)
    if kind is TitleKind.movie and year:
        return f"{title} {year}"
    if kind is TitleKind.series and season is not None:
        if episode is not None:
            return f"{title} S{season:02d}E{episode:02d}"
        return f"{title} S{season:02d}"
    return title


def season_words_query(title: str, season: int) -> str:
    """The other way packs get named, for when ``build_query`` finds none."""

    return f"{search_title(title)} Season {season}"


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


def is_season_pack(release_name: str) -> bool:
    """A whole season: season number(s), no episode, and not one slice of a season
    ('Show S02 Part 1', 'Show S02 Disc 2')."""

    info = guessit(release_name)
    if info.get("part") is not None or _DISC.search(release_name):
        return False
    return info.get("season") is not None and info.get("episode") is None


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
    dead: frozenset[str] | None = None,
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

    ``dead`` infohashes went nowhere last time. They stay eligible, ranked last: a
    swarm can come back, and a poor chance beats none.

    ``movie`` rejects releases that parse as a TV episode/season — a movie search
    ("Digger 2026") otherwise matches an episode whose *title* contains the word
    ("Moonshiners S16E04 Digger Knows Best").

    ``expected_title`` must be the release's whole title, for movies and seasons alike.
    With ``expected_year``, a movie one year off (TMDb and release dates differ) ranks
    below an exact year and a release with no year below both.

    Sizes are checked by what's being grabbed: movie, episode or pack.

    ``require_hd`` (default) rejects cam/telesync/telecine/screener sources, SD
    containers (avi/xvid/divx), and releases with no detectable HD resolution —
    so a fake "WEB-DL [Foreign Dub]" with no resolution, or a TC/CAM rip, is
    never grabbed. Turn it off to accept lower-quality or unlabeled releases.
    """

    allowed = [r.lower() for r in allowed_resolutions] if allowed_resolutions else []
    size_bounds = None
    if movie:
        size_bounds = _MOVIE_SIZE
    elif season is not None:
        size_bounds = _PACK_SIZE if episode is None else _EPISODE_SIZE
    scored: list[tuple[int, int, int, int, TorrentRelease]] = []
    for rel in releases:
        if rel.magnet_uri() is None or rel.seeders < min_seeders:
            continue
        if _EXECUTABLE_NAME.search(rel.title):
            continue  # malware bait, whatever its seeders say
        infohash = rel.btih()
        if banned and infohash in banned:
            continue  # quarantined before; the same fake resurfaces in searches
        if not _sane_size(rel, size_bounds):
            continue
        tags = _quality_text(rel.title, expected_title)
        if require_hd and (_LOW_QUALITY.search(tags) or _SD_CONTAINER.search(tags)):
            continue  # cam/ts/tc/screener or avi/xvid — never real HD
        year_rank = 0
        if movie:
            seasons, episodes = parse_season_episode(tags)
            if seasons or episodes:
                continue  # a TV episode/season, not the movie we asked for
            if expected_title and not _release_is_title(rel.title, expected_title):
                continue  # the movie name appears, but it's a different release
            if expected_year is not None:
                ry = _release_year(rel.title, expected_title)
                if ry is None:
                    year_rank = 2  # can't rule it out, so only if nothing better turns up
                elif abs(ry - expected_year) > 1:
                    continue  # a different film sharing the title (Digger 2020 vs 2026)
                elif ry != expected_year:
                    year_rank = 1
        if season is not None:
            if expected_title and not _release_is_title(rel.title, expected_title, series=True):
                continue  # right season number, wrong show
            seasons, episodes = parse_season_episode(rel.title)
            if seasons != [season]:
                continue
            if episode is None:
                if not is_season_pack(rel.title):  # an episode or a slice, not the pack
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
        # Lower rank = better: a hash that didn't go dead, then the surest year, then
        # preferred resolutions (by the operator's ordering, unknown last); then more
        # seeders wins.
        res_rank = allowed.index(res) if (allowed and res in allowed) else len(allowed)
        dead_rank = 1 if dead and infohash in dead else 0
        scored.append((dead_rank, year_rank, res_rank, -rel.seeders, rel))
    if not scored:
        return None
    scored.sort(key=lambda t: t[:4])
    return scored[0][4]
