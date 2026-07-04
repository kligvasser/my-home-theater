"""1337x via HTML scraping.

1337x has no API: search returns an HTML table (name + seeders/leechers/size),
and the magnet lives on each torrent's detail page — so getting a magnet costs a
second request. The site sits behind Cloudflare, so every fetch goes through
:func:`fetch_html` (which uses FlareSolverr when configured and otherwise raises
``ChallengeError`` that we swallow into an empty result).

Because magnets need a per-release detail fetch, we resolve them only for the top
few rows by seeders — the selector never needs magnets for releases it won't pick.
Regex parsing (no bs4/lxml in the dependency set) is deliberately defensive: a
layout change yields fewer/zero results, never a crash.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import quote

import httpx

from ....db.models import TitleKind
from ....logging_setup import get_logger
from ..base import TorrentRelease
from ..http import ChallengeError, fetch_html

log = get_logger(__name__)

# Cap detail-page fetches per search (each is a Cloudflare solve). The selector
# only needs a magnet for its single winner, so a handful of top-seeded rows is
# plenty of headroom.
_MAX_DETAIL_FETCHES = 8

_ROW_RE = re.compile(
    r'<a href="(?P<href>/torrent/[^"]+)"[^>]*>(?P<name>[^<]+)</a>.*?'
    r'coll-2 seeds"[^>]*>(?P<seeds>\d+).*?'
    r'coll-3 leeches"[^>]*>(?P<leech>\d+).*?'
    r"coll-4 size[^>]*>(?P<size>[\d.,]+\s*[KMGT]?i?B)",
    re.IGNORECASE | re.DOTALL,
)
_MAGNET_RE = re.compile(r'href="(magnet:\?[^"]+)"', re.IGNORECASE)

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


class X1337Source:
    name = "1337x"

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        *,
        flaresolverr_url: str | None,
        timeout: float,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = client
        self._flaresolverr = flaresolverr_url
        self._timeout = timeout

    async def search(self, query: str, kind: TitleKind) -> list[TorrentRelease]:
        url = f"{self._base}/search/{quote(query)}/1/"
        try:
            html = await fetch_html(
                self._client, url, flaresolverr_url=self._flaresolverr, timeout=self._timeout
            )
        except ChallengeError as exc:
            log.warning("torrent.1337x.blocked", detail=str(exc))
            return []

        rows = list(self._parse_rows(html))
        rows.sort(key=lambda r: r[1], reverse=True)  # by seeders, so we resolve the best
        out: list[TorrentRelease] = []
        for href, seeds, leech, size in rows[:_MAX_DETAIL_FETCHES]:
            magnet = await self._resolve_magnet(href)
            if magnet is None:
                continue
            out.append(
                TorrentRelease(
                    source=self.name,
                    title=_name_from_href(href),
                    seeders=seeds,
                    leechers=leech,
                    size_bytes=size,
                    magnet=magnet,
                )
            )
        return out

    def _parse_rows(self, html: str) -> list[tuple[str, int, int, int | None]]:
        rows: list[tuple[str, int, int, int | None]] = []
        for m in _ROW_RE.finditer(html):
            rows.append(
                (
                    m.group("href"),
                    int(m.group("seeds")),
                    int(m.group("leech")),
                    _parse_size(m.group("size")),
                )
            )
        return rows

    async def _resolve_magnet(self, href: str) -> str | None:
        try:
            html = await fetch_html(
                self._client,
                f"{self._base}{href}",
                flaresolverr_url=self._flaresolverr,
                timeout=self._timeout,
            )
        except (ChallengeError, httpx.HTTPError) as exc:
            log.warning("torrent.1337x.detail_failed", href=href, detail=str(exc))
            return None
        match = _MAGNET_RE.search(html)
        # Decode HTML entities (&amp; -> &) so tracker/dn params aren't garbled.
        return unescape(match.group(1)) if match else None


def _name_from_href(href: str) -> str:
    # /torrent/12345/Some-Release-Name-1080p/ -> "Some Release Name 1080p"
    parts = [p for p in href.split("/") if p]
    slug = parts[-1] if parts else href
    return slug.replace("-", " ").replace(".", " ").strip()


def _parse_size(text: str) -> int | None:
    match = re.match(r"([\d.,]+)\s*([KMGT]?i?B)", text.strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    unit = match.group(2).upper().replace("I", "")  # GiB -> GB
    return int(number * _SIZE_UNITS.get(unit, 1))
