"""opensubtitles.org via the legacy XML-RPC API.

This is the old platform's API (separate from the .com REST provider). Its draw is
avoiding the .com daily download cap; the trade-off is that it's deprecated,
rate-limited, and requires a *registered* User-Agent (the temporary one is
throttled). We keep the transport async by serializing/parsing XML-RPC with the
stdlib :mod:`xmlrpc.client` but POSTing through httpx.

Matching sends separate criteria for the movie hash + byte size and for the imdb id
(+ season/episode for series), because within one criteria dict the server honours
the hash and ignores the rest; a text query is the last resort. Download links are
gzip.

Docs: https://trac.opensubtitles.org/projects/opensubtitles/wiki/XmlRpcIntro
"""

from __future__ import annotations

import asyncio
import time
import xml.parsers.expat
import xmlrpc.client
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

_ENDPOINT = "https://api.opensubtitles.org/xml-rpc"
# Status codes from the XML-RPC body; HTTP itself is usually 200.
_AUTH_STATUSES = ("401", "402", "406", "411", "414", "415")  # bad login/session/user agent
_QUOTA_STATUSES = ("407", "429")  # download limit, rate limit

# ISO-639-1 -> ISO-639-2/B, which .org's ``sublanguageid`` expects.
_LANG3 = {
    "he": "heb",
    "en": "eng",
    "ar": "ara",
    "es": "spa",
    "fr": "fre",
    "de": "ger",
    "ru": "rus",
    "it": "ita",
    "pt": "por",
    "nl": "dut",
    "pl": "pol",
    "tr": "tur",
}


class OpenSubtitlesOrgError(SubtitleSourceError):
    pass


class OpenSubtitlesOrgSource:
    name = "opensubtitles_org"

    def __init__(
        self,
        username: str,
        password: str,
        client: httpx.AsyncClient,
        *,
        user_agent: str = "TemporaryUserAgent",
        timeout: float = 20.0,
        min_interval: float = 0.5,  # .org allows about 40 calls per 10 seconds
    ) -> None:
        self._username = username
        self._password = password
        self._client = client
        self._ua = user_agent
        self._timeout = timeout
        self._token: str | None = None
        self._min_interval = min_interval
        self._last_call = 0.0

    def supports(self, lang: str) -> bool:
        return lang in _LANG3

    async def _pace(self) -> None:
        wait = self._last_call + self._min_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def _call(self, method: str, *params: Any) -> dict[str, Any]:
        await self._pace()
        body = xmlrpc.client.dumps(params, method).encode("utf-8")
        resp = await self._client.post(
            _ENDPOINT,
            content=body,
            headers={"Content-Type": "text/xml", "User-Agent": self._ua},
            timeout=self._timeout,
        )
        raise_for_http(resp, self.name)
        try:
            (result,), _ = xmlrpc.client.loads(resp.text)
        except (xmlrpc.client.Error, xml.parsers.expat.ExpatError, ValueError) as exc:
            raise OpenSubtitlesOrgError(f"{method}: unreadable response") from exc
        if not isinstance(result, dict):
            raise OpenSubtitlesOrgError(f"{method}: unexpected response")
        _check_status(method, result)
        return result

    async def _login(self) -> str:
        if self._token:
            return self._token
        res = await self._call("LogIn", self._username, self._password, "en", self._ua)
        token = res.get("token")
        if not token:
            raise SourceAuthError("opensubtitles_org: login returned no token")
        self._token = str(token)
        return self._token

    def _criteria(self, query: SubtitleQuery, lang3: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if query.moviehash and query.filesize:
            out.append(
                {
                    "sublanguageid": lang3,
                    "moviehash": query.moviehash,
                    "moviebytesize": str(query.filesize),
                }
            )
        by_id: dict[str, Any] = {"sublanguageid": lang3}
        if query.imdb_id:
            by_id["imdbid"] = str(_imdb_num(query.imdb_id))
        elif not out:
            by_id["query"] = query.title
        else:
            return out  # hash, no imdb id: a bare title could only add wrong films
        if query.kind is TitleKind.series:
            by_id["season"] = str(query.season)
            by_id["episode"] = str(query.episode)
        out.append(by_id)
        return out

    async def search(self, query: SubtitleQuery) -> list[SubtitleResult]:
        lang3 = _LANG3.get(query.lang)
        if lang3 is None or not query.searchable:
            return []
        token = await self._login()
        res = await self._call("SearchSubtitles", token, self._criteria(query, lang3))
        data = res.get("data")
        if not data:  # .org returns False (not []) when there are no matches
            return []
        best: dict[str, SubtitleResult] = {}  # the criteria overlap: keep each link once
        for item in data:
            link = item.get("SubDownloadLink")
            if not link or _is_unwanted(item):
                continue
            # Prefer a hash match, then popularity.
            hash_match = item.get("MatchedBy") == "moviehash"
            score = (1_000_000 if hash_match else 0) + _int(item.get("SubDownloadsCnt"))
            if link in best and best[link].score >= score:
                continue
            best[link] = SubtitleResult(
                source=self.name,
                lang=query.lang,
                name=str(item.get("MovieReleaseName") or item.get("SubFileName", "")),
                score=float(score),
                ref={"link": link},
                hearing_impaired=str(item.get("SubHearingImpaired", "0")) == "1",
                hash_match=hash_match,
                season=query.season,
                episode=query.episode,
            )
        return list(best.values())

    async def download(self, result: SubtitleResult) -> bytes:
        data = await fetch_capped(
            self._client,
            result.ref["link"],
            source=self.name,
            timeout=self._timeout,
            headers={"User-Agent": self._ua},
        )
        return unpack_subtitle(data, result.season, result.episode)


def _check_status(method: str, res: dict[str, Any]) -> None:
    """HTTP 200 proves nothing here: the real outcome is the body's ``status``."""

    status = str(res.get("status", ""))
    if status.startswith("200"):
        return
    code = status[:3]
    detail = f"opensubtitles_org: {method} returned status {code or 'none'}"
    if code in _AUTH_STATUSES:
        raise SourceAuthError(detail)
    if code in _QUOTA_STATUSES:
        raise SourceQuotaError(detail)
    raise OpenSubtitlesOrgError(detail)


def _is_unwanted(item: dict[str, Any]) -> bool:
    """Flagged bad by moderators, machine-translated, or foreign-parts-only."""

    return any(
        str(item.get(flag, "0")) == "1"
        for flag in ("SubBad", "SubAutoTranslation", "SubForeignPartsOnly")
    )


def _imdb_num(imdb_id: str) -> int:
    return int(imdb_id.lower().removeprefix("tt").lstrip("0") or "0")


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
