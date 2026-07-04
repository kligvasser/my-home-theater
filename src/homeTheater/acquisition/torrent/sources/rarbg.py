"""RARBG (clone) — best-effort, experimental.

The original RARBG shut down in 2023; every site using the name today is an
unofficial clone with an unknown, unstable layout. This client scrapes magnets
from a clone's search page and is deliberately conservative: if it can't find
recognizable results it returns an empty list rather than guessing. Treat it as a
bonus source, not a dependable one — keep "piratebay" enabled alongside it.
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

# Grab magnets plus the name we can recover from the magnet's own dn= field. We
# can't reliably parse seeders/size from an unknown clone layout, so those default
# to 0/None and such releases simply rank below sources that report seeders.
_MAGNET_RE = re.compile(r'href="(magnet:\?[^"]+)"', re.IGNORECASE)
_DN_RE = re.compile(r"[?&]dn=([^&]+)", re.IGNORECASE)


class RarbgSource:
    name = "rarbg"

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
        url = f"{self._base}/search/?search={quote(query)}"
        try:
            html = await fetch_html(
                self._client, url, flaresolverr_url=self._flaresolverr, timeout=self._timeout
            )
        except (ChallengeError, httpx.HTTPError) as exc:
            log.warning("torrent.rarbg.unavailable", detail=str(exc))
            return []

        out: list[TorrentRelease] = []
        seen: set[str] = set()
        for match in _MAGNET_RE.finditer(html):
            magnet = unescape(match.group(1))  # decode &amp; in tracker params
            title = _title_from_magnet(magnet)
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                TorrentRelease(
                    source=self.name,
                    title=title,
                    seeders=0,
                    leechers=0,
                    size_bytes=None,
                    magnet=magnet,
                )
            )
        if not out:
            log.info("torrent.rarbg.no_results", query=query)
        return out


def _title_from_magnet(magnet: str) -> str:
    match = _DN_RE.search(magnet)
    if not match:
        return "unknown"
    from urllib.parse import unquote_plus

    return unquote_plus(match.group(1))
