"""Query building + release selection for the torrent backend.

Given a title and a bag of search hits from all enabled sources, pick the one
release to grab: drop under-seeded and wrong-resolution releases, then rank by
resolution preference (the operator's ``allowed_resolutions`` order) and seeders.
"""

from __future__ import annotations

from guessit import guessit

from ...db.models import TitleKind
from .base import TorrentRelease


def build_query(title: str, year: int | None, kind: TitleKind) -> str:
    """A search string broad enough to get hits but specific enough to be relevant.

    Movies add the year (cheap disambiguation); series don't, since a season/pack
    release rarely carries the first-air year in its name.
    """

    title = title.strip()
    if kind is TitleKind.movie and year:
        return f"{title} {year}"
    return title


def detect_resolution(release_name: str) -> str | None:
    """Normalised resolution parsed from a release name (e.g. '1080p', '2160p')."""

    res = guessit(release_name).get("screen_size")
    if not res:
        return None
    res = str(res).lower()
    return "2160p" if res in {"4k", "uhd"} else res


def select_release(
    releases: list[TorrentRelease],
    *,
    allowed_resolutions: list[str],
    min_seeders: int,
) -> TorrentRelease | None:
    """Best downloadable release, or ``None`` if nothing qualifies.

    A release is dropped when it has no usable magnet, too few seeders, or a
    *detected* resolution outside ``allowed_resolutions``. Releases whose
    resolution can't be parsed are kept but ranked below explicit matches.
    """

    allowed = [r.lower() for r in allowed_resolutions] if allowed_resolutions else []
    scored: list[tuple[int, int, TorrentRelease]] = []
    for rel in releases:
        if rel.magnet_uri() is None or rel.seeders < min_seeders:
            continue
        res = detect_resolution(rel.title)
        if allowed and res is not None and res not in allowed:
            continue
        # Lower rank = better: preferred resolutions first (by the operator's
        # ordering), unknown-resolution last; then more seeders wins.
        res_rank = allowed.index(res) if (allowed and res in allowed) else len(allowed)
        scored.append((res_rank, -rel.seeders, rel))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][2]
