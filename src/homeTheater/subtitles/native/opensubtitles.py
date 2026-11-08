"""OpenSubtitles.com REST API v1 provider.

Search needs only the API key; downloading needs a logged-in bearer token (the
free tier allows a handful of downloads/day). Best matches come from the file's
``moviehash``; we fall back to imdb id, then a text query. For episodes we search
by the *series* imdb id + season/episode numbers.

Docs: https://opensubtitles.stoplight.io/docs/opensubtitles-api
"""

from __future__ import annotations

from typing import Any

import httpx

from ...db.models import TitleKind
from ...logging_setup import get_logger
from .base import (
    SourceAuthError,
    SourceQuotaError,
    SubtitleQuery,
    SubtitleResult,
    SubtitleSourceError,
    raise_for_http,
)
from .payload import fetch_capped, unpack_subtitle

log = get_logger(__name__)

_BASE = "https://api.opensubtitles.com/api/v1"
# 406 on /download: the account's daily allowance is used up.
_QUOTA_CODES = (406, 429)


class OpenSubtitlesError(SubtitleSourceError):
    pass


class OpenSubtitlesSource:
    name = "opensubtitles"

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        *,
        username: str | None = None,
        password: str | None = None,
        user_agent: str = "my-home-theater v1",
        timeout: float = 20.0,
    ) -> None:
        self._key = api_key
        self._client = client
        self._username = username
        self._password = password
        self._ua = user_agent
        self._timeout = timeout
        self._token: str | None = None
        self._quota_spent = False

    def supports(self, lang: str) -> bool:
        return True  # OpenSubtitles covers a broad language set

    def _headers(self, *, auth: bool = False) -> dict[str, str]:
        h = {
            "Api-Key": self._key,
            "User-Agent": self._ua,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if auth and self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _login(self) -> None:
        if self._token or not (self._username and self._password):
            return
        resp = await self._client.post(
            f"{_BASE}/login",
            json={"username": self._username, "password": self._password},
            headers=self._headers(),
            timeout=self._timeout,
        )
        raise_for_http(resp, self.name)
        self._token = resp.json().get("token")
        if not self._token:
            raise SourceAuthError("opensubtitles: login returned no token")

    def _require_quota(self) -> None:
        if self._quota_spent:
            raise SourceQuotaError("opensubtitles: daily download quota is used up")

    async def search(self, query: SubtitleQuery) -> list[SubtitleResult]:
        self._require_quota()  # no point searching for what can't be downloaded
        if not query.searchable:
            return []
        params: dict[str, Any] = {"languages": query.lang}
        if query.moviehash:
            params["moviehash"] = query.moviehash
        if query.kind is TitleKind.series:
            # Always pin the episode; use the series imdb id when known, else the
            # title text — never drop season/episode (that grabs a random episode).
            if query.imdb_id:
                params["parent_imdb_id"] = _imdb_num(query.imdb_id)
            else:
                params["query"] = query.title
            params["season_number"] = query.season
            params["episode_number"] = query.episode
        elif query.imdb_id:
            params["imdb_id"] = _imdb_num(query.imdb_id)
        else:
            params["query"] = query.title

        # OpenSubtitles requires query params sorted alphabetically and lower-case
        # values, else it 301-redirects to the canonical form (docs "best practices").
        canonical = {k: str(v).lower() for k, v in sorted(params.items())}
        resp = await self._client.get(
            f"{_BASE}/subtitles",
            params=canonical,
            headers=self._headers(),
            timeout=self._timeout,
            follow_redirects=True,
        )
        raise_for_http(resp, self.name)
        out: list[SubtitleResult] = []
        for item in resp.json().get("data", []):
            attrs = item.get("attributes", {})
            if (
                attrs.get("ai_translated")
                or attrs.get("machine_translated")
                or attrs.get("foreign_parts_only")
            ):
                continue
            files = attrs.get("files") or []
            if not files:
                continue
            file_id = files[0].get("file_id")
            if file_id is None:
                continue
            # Rank: a hash match is decisive; otherwise popularity.
            hash_match = bool(attrs.get("moviehash_match"))
            score = (1_000_000 if hash_match else 0) + float(attrs.get("download_count", 0) or 0)
            out.append(
                SubtitleResult(
                    source=self.name,
                    lang=str(attrs.get("language", query.lang)).lower(),
                    name=str(attrs.get("release") or files[0].get("file_name") or ""),
                    score=score,
                    ref={"file_id": file_id},
                    hearing_impaired=bool(attrs.get("hearing_impaired")),
                    hash_match=hash_match,
                    season=query.season,
                    episode=query.episode,
                )
            )
        return out

    async def download(self, result: SubtitleResult) -> bytes:
        self._require_quota()
        await self._login()
        resp = await self._client.post(
            f"{_BASE}/download",
            json={"file_id": result.ref["file_id"]},
            headers=self._headers(auth=True),
            timeout=self._timeout,
        )
        raise_for_http(resp, self.name, quota_codes=_QUOTA_CODES)
        body = resp.json()
        remaining = body.get("remaining")
        if isinstance(remaining, int) and remaining <= 0:
            self._quota_spent = True  # this one is the last; the link is still good
            log.info("opensubtitles.quota_spent", reset=body.get("reset_time"))
        link = body.get("link")
        if not link:
            if self._quota_spent:
                raise SourceQuotaError("opensubtitles: daily download quota is used up")
            raise OpenSubtitlesError("opensubtitles: no download link in the response")
        data = await fetch_capped(self._client, link, source=self.name, timeout=self._timeout)
        return unpack_subtitle(data, result.season, result.episode)


def _imdb_num(imdb_id: str) -> int:
    return int(imdb_id.lower().removeprefix("tt").lstrip("0") or "0")
