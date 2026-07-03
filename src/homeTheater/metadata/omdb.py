"""OMDb client: IMDb rating + votes by imdb_id.

OMDb's free tier is 1,000 requests/day, so responses are cached. Note the quirky
serialization: ``imdbVotes`` is a string like ``"1,234,567"`` and either field
can be ``"N/A"`` — parsed here into clean numbers.
"""

from __future__ import annotations

from typing import Any

import httpx

from .cache import cache_get, cache_set
from .dto import OmdbRatings
from .http import get_json

BASE_URL = "https://www.omdbapi.com/"
PROVIDER = "omdb"


def parse_rating(value: Any) -> float | None:
    """``"7.8"`` -> 7.8; ``"N/A"``/missing -> None."""

    if not value or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_votes(value: Any) -> int | None:
    """``"1,234,567"`` -> 1234567; ``"N/A"``/missing -> None."""

    if not value or value == "N/A":
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class OMDbClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient, cache_days: int = 14) -> None:
        self._api_key = api_key
        self._client = client
        self._cache_days = cache_days

    async def by_imdb_id(self, imdb_id: str) -> OmdbRatings:
        key = f"rating:{imdb_id}"
        cached = cache_get(PROVIDER, key, self._cache_days)
        if cached is None:
            cached = await get_json(
                self._client,
                BASE_URL,
                {"apikey": self._api_key, "i": imdb_id},
            )
            cache_set(PROVIDER, key, cached)

        if cached.get("Response") == "False":
            return OmdbRatings()
        return OmdbRatings(
            imdb_rating=parse_rating(cached.get("imdbRating")),
            imdb_votes=parse_votes(cached.get("imdbVotes")),
        )
