"""opensubtitles.org via the legacy XML-RPC API.

This is the old platform's API (separate from the .com REST provider). Its draw is
avoiding the .com daily download cap; the trade-off is that it's deprecated,
rate-limited, and requires a *registered* User-Agent (the temporary one is
throttled). We keep the transport async by serializing/parsing XML-RPC with the
stdlib :mod:`xmlrpc.client` but POSTing through httpx.

Matching uses the movie hash + byte size when available (most accurate), else the
imdb id (+ season/episode for series), else a text query. Download links are gzip.

Docs: https://trac.opensubtitles.org/projects/opensubtitles/wiki/XmlRpcIntro
"""

from __future__ import annotations

import gzip
import xmlrpc.client
from typing import Any

import httpx

from ...db.models import TitleKind
from ...logging_setup import get_logger
from .base import SubtitleQuery, SubtitleResult

log = get_logger(__name__)

_ENDPOINT = "https://api.opensubtitles.org/xml-rpc"

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


class OpenSubtitlesOrgError(RuntimeError):
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
    ) -> None:
        self._username = username
        self._password = password
        self._client = client
        self._ua = user_agent
        self._timeout = timeout
        self._token: str | None = None

    def supports(self, lang: str) -> bool:
        return lang in _LANG3

    async def _call(self, method: str, *params: Any) -> dict[str, Any]:
        body = xmlrpc.client.dumps(params, method).encode("utf-8")
        resp = await self._client.post(
            _ENDPOINT,
            content=body,
            headers={"Content-Type": "text/xml", "User-Agent": self._ua},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        (result,), _ = xmlrpc.client.loads(resp.text)
        return result if isinstance(result, dict) else {}

    async def _login(self) -> str:
        if self._token:
            return self._token
        res = await self._call("LogIn", self._username, self._password, "en", self._ua)
        token = res.get("token")
        if not token:
            raise OpenSubtitlesOrgError(f"login failed: {res.get('status', 'no token')}")
        self._token = str(token)
        return self._token

    async def search(self, query: SubtitleQuery) -> list[SubtitleResult]:
        lang3 = _LANG3.get(query.lang)
        if lang3 is None:
            return []
        token = await self._login()
        req: dict[str, Any] = {"sublanguageid": lang3}
        if query.moviehash and query.filesize:
            req["moviehash"] = query.moviehash
            req["moviebytesize"] = str(query.filesize)
        if query.imdb_id:
            req["imdbid"] = str(_imdb_num(query.imdb_id))
        if query.kind is TitleKind.series and query.season is not None:
            req["season"] = str(query.season)
            if query.episode is not None:
                req["episode"] = str(query.episode)
        if "imdbid" not in req and "moviehash" not in req:
            req["query"] = query.title

        res = await self._call("SearchSubtitles", token, [req])
        data = res.get("data")
        if not data:  # .org returns False (not []) when there are no matches
            return []
        out: list[SubtitleResult] = []
        for item in data:
            link = item.get("SubDownloadLink")
            if not link:
                continue
            # Prefer a hash match, then popularity.
            score = (1_000_000 if item.get("MatchedBy") == "moviehash" else 0) + _int(
                item.get("SubDownloadsCnt")
            )
            out.append(
                SubtitleResult(
                    source=self.name,
                    lang=query.lang,
                    name=str(item.get("SubFileName", "")),
                    score=float(score),
                    ref={"link": link},
                    hearing_impaired=str(item.get("SubHearingImpaired", "0")) == "1",
                )
            )
        return out

    async def download(self, result: SubtitleResult) -> bytes:
        resp = await self._client.get(result.ref["link"], timeout=self._timeout)
        resp.raise_for_status()
        data = resp.content
        try:
            return gzip.decompress(data)
        except (OSError, EOFError):
            return data  # already plain (some mirrors don't gzip)


def _imdb_num(imdb_id: str) -> int:
    return int(imdb_id.lower().removeprefix("tt").lstrip("0") or "0")


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
