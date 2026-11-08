"""Choose among a provider's results for one media file.

Order: a moviehash match is decisive; then how well the subtitle's release matches
the file (same group/source/resolution/codec means it is very likely in sync) plus
the configured hearing-impaired preference; then the provider's own popularity.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from guessit import guessit

from .base import SubtitleResult

_TOKEN_WEIGHTS = (("release_group", 4), ("source", 3), ("screen_size", 2), ("video_codec", 1))
_HEARING_IMPAIRED_WEIGHT = 2


@lru_cache(maxsize=4096)
def _release_tokens(name: str) -> tuple[tuple[str, str], ...]:
    try:
        info: dict[str, Any] = dict(guessit(name))
    except Exception:  # guessit raises its own errors on odd names; no tokens then
        return ()
    return tuple(
        (key, str(info[key]).casefold()) for key, _ in _TOKEN_WEIGHTS if info.get(key) is not None
    )


def release_overlap(media_name: str, subtitle_name: str) -> int:
    """Weighted count of release tokens the two names share."""

    media = dict(_release_tokens(media_name))
    theirs = dict(_release_tokens(subtitle_name))
    return sum(
        weight for key, weight in _TOKEN_WEIGHTS if key in media and media[key] == theirs.get(key)
    )


def rank_results(
    results: list[SubtitleResult], release_name: str, *, hearing_impaired: bool
) -> list[SubtitleResult]:
    """``results`` best first. CPU-bound (guessit): call it off the event loop."""

    def key(result: SubtitleResult) -> tuple[bool, int, float]:
        fit = release_overlap(release_name, result.name)
        if result.hearing_impaired == hearing_impaired:
            fit += _HEARING_IMPAIRED_WEIGHT
        return (result.hash_match, fit, result.score)

    return sorted(results, key=key, reverse=True)
