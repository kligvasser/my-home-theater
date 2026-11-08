"""TMDb client: search, details (with external ids, genres, and taste features),
and discovery lists.

A v3 API key can only be sent as a query param, so ``http.get_json`` strips it from
any exception. A v4 read access token (a JWT, also accepted as ``TMDB_API_KEY``)
goes in the ``Authorization`` header and never reaches a URL. Responses are cached
via :mod:`.cache` to respect rate limits. Discovery lists (trending /
top-rated) use a date-stamped cache key with a 1-day TTL — "trending this week"
must not be served stale for the full metadata ``cache_days``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from ..db.base import utcnow
from ..db.models import TitleKind
from .cache import cache_get, cache_set
from .dto import TmdbSeason, TmdbTitle
from .http import get_json
from .names import names_match

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
PROVIDER = "tmdb"
PAGE_SIZE = 20  # fixed by the TMDb API
MAX_LIST_PAGES = 5
LIST_TTL_DAYS = 1
# Results per search, and in all per title, worth one extra request for their
# translated and alternative names when no primary name matched.
NAME_LOOKUPS_PER_SEARCH = 3
MAX_NAME_LOOKUPS = 5

# Details payloads changed shape when taste features were added; the version tag
# keeps stale pre-feature cache entries from being served.
_DETAILS_CACHE_VERSION = "v2"


def _year_of(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _poster(path: str | None) -> str | None:
    return f"{IMAGE_BASE}{path}" if path else None


def _int_or_none(value: Any) -> int | None:
    """Lenient int coercion: TMDb occasionally returns numeric strings/floats."""

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _movie_certification(data: dict[str, Any]) -> str | None:
    for entry in (data.get("release_dates") or {}).get("results") or []:
        if entry.get("iso_3166_1") == "US":
            for rel in entry.get("release_dates") or []:
                cert = (rel.get("certification") or "").strip()
                if cert:
                    return cert
    return None


def _tv_certification(data: dict[str, Any]) -> str | None:
    for entry in (data.get("content_ratings") or {}).get("results") or []:
        if entry.get("iso_3166_1") == "US":
            rating = (entry.get("rating") or "").strip()
            if rating:
                return rating
    return None


def _has_results(data: dict[str, Any]) -> bool:
    return bool(data.get("results"))


def _names(items: list[dict[str, Any]] | None, limit: int | None = None) -> list[str]:
    out = [i["name"] for i in (items or []) if i.get("name")]
    return out[:limit] if limit else out


class TMDbClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        language: str = "en-US",
        cache_days: int = 14,
    ) -> None:
        self._client = client
        # JWT-shaped means a v4 token; v3 keys are bare hex and only work as a param.
        self._bearer = api_key.count(".") == 2
        self._auth_params = {} if self._bearer else {"api_key": api_key}
        self._headers = {"Authorization": f"Bearer {api_key}"} if self._bearer else None
        self._language = language
        self._cache_days = cache_days

    async def _get(
        self,
        path: str,
        params: dict[str, Any],
        cache_key: str,
        ttl_days: int | None = None,
        keep: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """GET through the cache. A payload failing ``keep`` is neither stored nor
        served from an older entry."""

        ttl = self._cache_days if ttl_days is None else min(ttl_days, self._cache_days)
        cached = cache_get(PROVIDER, cache_key, ttl)
        if cached is not None and (keep is None or keep(cached)):
            return cached
        data = await get_json(
            self._client,
            f"{BASE_URL}{path}",
            {**self._auth_params, **params},
            headers=self._headers,
        )
        if keep is None or keep(data):
            cache_set(PROVIDER, cache_key, data)
        return data

    async def _search_page(
        self, title: str, year: int | None, kind: TitleKind
    ) -> list[dict[str, Any]]:
        if kind is TitleKind.movie:
            path, year_param = "/search/movie", "year"
        else:
            path, year_param = "/search/tv", "first_air_date_year"
        params: dict[str, Any] = {"query": title, "language": self._language}
        if year:
            params[year_param] = year
        key = f"search:{kind.value}:{self._language}:{title.lower()}:{year or ''}"
        # An empty answer is not cached: the title may simply not be on TMDb *yet*,
        # and two weeks of remembered "nothing" is how it stayed unmatched.
        data = await self._get(path, params, key, keep=_has_results)
        return list(data.get("results") or [])

    async def _all_names(self, tmdb_id: int, kind: TitleKind) -> list[str | None]:
        """Every name TMDb knows for a title, translations and aliases included."""

        path = f"/movie/{tmdb_id}" if kind is TitleKind.movie else f"/tv/{tmdb_id}"
        data = await self._get(
            path,
            {"append_to_response": "alternative_titles,translations"},
            f"names:{kind.value}:{tmdb_id}",
        )
        names: list[str | None] = [
            data.get("title"),
            data.get("name"),
            data.get("original_title"),
            data.get("original_name"),
        ]
        # /movie keys these under "titles", /tv under "results".
        aliases = data.get("alternative_titles") or {}
        for alias in (aliases.get("titles") or []) + (aliases.get("results") or []):
            names.append(alias.get("title"))
        for entry in (data.get("translations") or {}).get("translations") or []:
            translated = entry.get("data") or {}
            names.append(translated.get("title") or translated.get("name"))
        return names

    @staticmethod
    def _pick(
        title: str, year: int | None, results: list[dict[str, Any]], kind: TitleKind
    ) -> int | None:
        """The result whose own name matches, preferring the nearest release year."""

        date_field = "release_date" if kind is TitleKind.movie else "first_air_date"
        name_fields = ("title", "original_title", "name", "original_name")

        def distance(result: dict[str, Any]) -> int:
            released = _year_of(result.get(date_field))
            return abs(released - year) if released is not None and year is not None else 99

        matching = [
            r
            for r in results
            if "id" in r
            and names_match(
                title,
                (r.get(field) for field in name_fields),
                exact_year=distance(r) == 0,
            )
        ]
        return int(min(matching, key=distance)["id"]) if matching else None

    async def search(self, title: str, year: int | None, kind: TitleKind) -> int | None:
        """The TMDb id of the title with this name, or None.

        A strict year search misses titles whose folder or filename is a year off
        (and those TMDb dates differently), so it widens to the neighbouring years
        and then to no year. A result only counts if a name matches: first its own
        names, then, for the top few, its translations and aliases, which is what
        keeps Hebrew and romanized titles matchable.
        """

        unverified: dict[int, None] = {}
        attempts = (year, year + 1, year - 1, None) if year else (None,)
        for attempt in attempts:
            results = await self._search_page(title, attempt, kind)
            match = self._pick(title, year, results, kind)
            if match is not None:
                return match
            for result in results[:NAME_LOOKUPS_PER_SEARCH]:
                if "id" in result:
                    unverified.setdefault(int(result["id"]))

        for tmdb_id in list(unverified)[:MAX_NAME_LOOKUPS]:
            if names_match(title, await self._all_names(tmdb_id, kind)):
                return tmdb_id
        return None

    async def search_results(self, title: str, kind: TitleKind, limit: int = 8) -> list[TmdbTitle]:
        """Top search matches as stubs (id/title/year/poster) — for the UI's
        search-and-add box, unlike :meth:`search` which picks one id."""

        if kind is TitleKind.movie:
            path, date_field = "/search/movie", "release_date"
        else:
            path, date_field = "/search/tv", "first_air_date"
        key = f"searchlist:{kind.value}:{self._language}:{title.lower()}"
        data = await self._get(
            path, {"query": title, "language": self._language}, key, keep=_has_results
        )
        out: list[TmdbTitle] = []
        for r in data.get("results") or []:
            if "id" not in r:
                continue
            out.append(
                TmdbTitle(
                    tmdb_id=int(r["id"]),
                    title=r.get("title") or r.get("name") or "",
                    year=_year_of(r.get(date_field)),
                    tmdb_rating=r.get("vote_average"),
                    tmdb_votes=r.get("vote_count"),
                    poster_url=_poster(r.get("poster_path")),
                    overview=r.get("overview") or None,
                )
            )
            if len(out) >= limit:
                break
        return out

    async def _discover_list(
        self, path: str, kind: TitleKind, cache_key: str, limit: int
    ) -> list[TmdbTitle]:
        date_field = "release_date" if kind is TitleKind.movie else "first_air_date"
        # Date-stamped key + short TTL: lists change daily, details don't.
        stamp = utcnow().date().isoformat()
        pages = min(MAX_LIST_PAGES, max(1, -(-limit // PAGE_SIZE)))  # ceil div
        out: list[TmdbTitle] = []
        for page in range(1, pages + 1):
            data = await self._get(
                path,
                {"language": self._language, "page": page},
                f"{cache_key}:{stamp}:p{page}",
                ttl_days=LIST_TTL_DAYS,
            )
            results = data.get("results") or []
            for r in results:
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
                        original_language=r.get("original_language"),
                    )
                )
            if len(out) >= limit or page >= int(data.get("total_pages") or 1):
                break
        return out[:limit]

    async def trending(
        self, kind: TitleKind, window: str = "week", limit: int = PAGE_SIZE
    ) -> list[TmdbTitle]:
        media = "movie" if kind is TitleKind.movie else "tv"
        return await self._discover_list(
            f"/trending/{media}/{window}", kind, f"trending:{media}:{window}", limit
        )

    async def top_rated(self, kind: TitleKind, limit: int = PAGE_SIZE) -> list[TmdbTitle]:
        media = "movie" if kind is TitleKind.movie else "tv"
        return await self._discover_list(f"/{media}/top_rated", kind, f"top_rated:{media}", limit)

    async def details(
        self, tmdb_id: int, kind: TitleKind, *, ttl_days: int | None = None
    ) -> TmdbTitle:
        """Full details. ``ttl_days`` shortens the cache for a caller that needs news."""

        if kind is TitleKind.movie:
            path = f"/movie/{tmdb_id}"
            extra = "external_ids,keywords,credits,release_dates"
        else:
            path = f"/tv/{tmdb_id}"
            extra = "external_ids,keywords,credits,content_ratings"
        key = f"details:{_DETAILS_CACHE_VERSION}:{kind.value}:{tmdb_id}:{self._language}"
        data = await self._get(
            path,
            {"language": self._language, "append_to_response": extra},
            key,
            ttl_days=ttl_days,
        )

        external = data.get("external_ids") or {}
        imdb_id = data.get("imdb_id") or external.get("imdb_id")
        tvdb_id = external.get("tvdb_id")
        credits = data.get("credits") or {}
        cast_top = _names(credits.get("cast"), limit=10)

        if kind is TitleKind.movie:
            name = data.get("title") or data.get("original_title") or ""
            release_date = data.get("release_date") or None
            runtime = data.get("runtime")
            countries = [
                c["iso_3166_1"]
                for c in (data.get("production_countries") or [])
                if c.get("iso_3166_1")
            ]
            certification = _movie_certification(data)
            keywords = _names((data.get("keywords") or {}).get("keywords"))
            directors = [
                c["name"]
                for c in (credits.get("crew") or [])
                if c.get("job") == "Director" and c.get("name")
            ]
            seasons = episodes = None
            series_status = None
            season_list = []
            last_aired: dict[str, Any] = {}
        else:
            name = data.get("name") or data.get("original_name") or ""
            release_date = data.get("first_air_date") or None
            run_times = data.get("episode_run_time") or []
            runtime = run_times[0] if run_times else None
            countries = [c for c in (data.get("origin_country") or []) if c]
            certification = _tv_certification(data)
            keywords = _names((data.get("keywords") or {}).get("results"))
            directors = _names(data.get("created_by"))
            seasons = _int_or_none(data.get("number_of_seasons"))
            episodes = _int_or_none(data.get("number_of_episodes"))
            series_status = data.get("status") or None
            season_list = [
                TmdbSeason(
                    number=n,
                    episode_count=_int_or_none(s.get("episode_count")),
                    air_date=s.get("air_date") or None,
                )
                for s in (data.get("seasons") or [])
                if (n := _int_or_none(s.get("season_number"))) is not None
            ]
            last_aired = data.get("last_episode_to_air") or {}

        collection = data.get("belongs_to_collection") or {}
        genres = [g["name"] for g in (data.get("genres") or []) if g.get("name")]

        return TmdbTitle(
            tmdb_id=tmdb_id,
            title=name,
            imdb_id=imdb_id or None,
            tvdb_id=_int_or_none(tvdb_id),
            year=_year_of(release_date),
            runtime=_int_or_none(runtime),
            genres=genres,
            tmdb_rating=data.get("vote_average"),
            tmdb_votes=data.get("vote_count"),
            popularity=data.get("popularity"),
            poster_url=_poster(data.get("poster_path")),
            overview=data.get("overview") or None,
            original_language=data.get("original_language"),
            origin_countries=countries,
            release_date=release_date,
            certification=certification,
            keywords=keywords,
            cast_top=cast_top,
            directors=directors,
            collection_tmdb_id=_int_or_none(collection.get("id")),
            collection_name=collection.get("name") or None,
            seasons_count=seasons,
            episodes_count=episodes,
            series_status=series_status,
            seasons=season_list,
            last_aired_season=_int_or_none(last_aired.get("season_number")),
            last_aired_episode=_int_or_none(last_aired.get("episode_number")),
        )
