"""TMDb client: search + details (with external ids and genres).

Uses the v3 API-key query param (works with a free TMDb key). Responses are
cached via :mod:`.cache` to respect rate limits.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..db.models import TitleKind
from .cache import cache_get, cache_set
from .dto import TmdbTitle
from .http import get_json

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
PROVIDER = "tmdb"


def _year_of(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _poster(path: str | None) -> str | None:
    return f"{IMAGE_BASE}{path}" if path else None


class TMDbClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        language: str = "en-US",
        cache_days: int = 14,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._language = language
        self._cache_days = cache_days

    async def _get(self, path: str, params: dict[str, Any], cache_key: str) -> dict[str, Any]:
        cached = cache_get(PROVIDER, cache_key, self._cache_days)
        if cached is not None:
            return cached
        params = {"api_key": self._api_key, **params}
        data = await get_json(self._client, f"{BASE_URL}{path}", params)
        cache_set(PROVIDER, cache_key, data)
        return data

    async def search(self, title: str, year: int | None, kind: TitleKind) -> int | None:
        """Return the best-matching TMDb id for a title, or None."""

        if kind is TitleKind.movie:
            params: dict[str, Any] = {"query": title, "language": self._language}
            if year:
                params["year"] = year
            path, date_field = "/search/movie", "release_date"
        else:
            params = {"query": title, "language": self._language}
            if year:
                params["first_air_date_year"] = year
            path, date_field = "/search/tv", "first_air_date"

        key = f"search:{kind.value}:{title.lower()}:{year or ''}"
        data = await self._get(path, params, key)
        results: list[dict[str, Any]] = data.get("results") or []
        if not results:
            return None

        # Prefer an exact-year match when a year is known; else the top result.
        if year is not None:
            for r in results:
                if _year_of(r.get(date_field)) == year:
                    return int(r["id"])
        return int(results[0]["id"])

    async def _discover_list(self, path: str, kind: TitleKind, cache_key: str) -> list[TmdbTitle]:
        data = await self._get(path, {"language": self._language}, cache_key)
        date_field = "release_date" if kind is TitleKind.movie else "first_air_date"
        out: list[TmdbTitle] = []
        for r in data.get("results") or []:
            if "id" not in r:
                continue
            name = r.get("title") or r.get("name") or ""
            out.append(
                TmdbTitle(
                    tmdb_id=int(r["id"]),
                    title=name,
                    year=_year_of(r.get(date_field)),
                    tmdb_rating=r.get("vote_average"),
                    tmdb_votes=r.get("vote_count"),
                    popularity=r.get("popularity"),
                    poster_url=_poster(r.get("poster_path")),
                    overview=r.get("overview") or None,
                )
            )
        return out

    async def trending(self, kind: TitleKind, window: str = "week") -> list[TmdbTitle]:
        media = "movie" if kind is TitleKind.movie else "tv"
        return await self._discover_list(
            f"/trending/{media}/{window}", kind, f"trending:{media}:{window}"
        )

    async def top_rated(self, kind: TitleKind) -> list[TmdbTitle]:
        media = "movie" if kind is TitleKind.movie else "tv"
        return await self._discover_list(f"/{media}/top_rated", kind, f"top_rated:{media}")

    async def details(self, tmdb_id: int, kind: TitleKind) -> TmdbTitle:
        path = f"/movie/{tmdb_id}" if kind is TitleKind.movie else f"/tv/{tmdb_id}"
        key = f"details:{kind.value}:{tmdb_id}:{self._language}"
        data = await self._get(
            path,
            {"language": self._language, "append_to_response": "external_ids"},
            key,
        )

        external = data.get("external_ids") or {}
        imdb_id = data.get("imdb_id") or external.get("imdb_id")
        tvdb_id = external.get("tvdb_id")

        if kind is TitleKind.movie:
            name = data.get("title") or data.get("original_title") or ""
            year = _year_of(data.get("release_date"))
            runtime = data.get("runtime")
        else:
            name = data.get("name") or data.get("original_name") or ""
            year = _year_of(data.get("first_air_date"))
            run_times = data.get("episode_run_time") or []
            runtime = run_times[0] if run_times else None

        genres = [g["name"] for g in (data.get("genres") or []) if g.get("name")]

        return TmdbTitle(
            tmdb_id=tmdb_id,
            title=name,
            imdb_id=imdb_id or None,
            tvdb_id=int(tvdb_id) if isinstance(tvdb_id, int) else None,
            year=year,
            runtime=int(runtime) if isinstance(runtime, int) else None,
            genres=genres,
            tmdb_rating=data.get("vote_average"),
            tmdb_votes=data.get("vote_count"),
            popularity=data.get("popularity"),
            poster_url=_poster(data.get("poster_path")),
            overview=data.get("overview") or None,
        )
