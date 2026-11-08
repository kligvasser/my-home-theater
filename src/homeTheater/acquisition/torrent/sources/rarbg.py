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
from ....errors import redact_exc
from ....logging_setup import get_logger
from ..base import TorrentRelease
from ..http import ChallengeError, fetch_html

log = get_logger(__name__)

# Grab magnets plus the name we can recover from the magnet's own dn= field.
# Seeders and size come from the classic table row (... | size | seeders | leechers |
# ...). A clone laid out differently falls back to 0/None, and min_seeders then
# drops the row: it's never a guess.
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SIZE_RE = re.compile(r"^([\d.,]+)\s*([KMGT]?)i?B$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
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
            log.warning("torrent.rarbg.unavailable", detail=redact_exc(exc))
            return []

        out: list[TorrentRelease] = []
        seen: set[str] = set()
        stats = _row_stats(html)
        for match in _MAGNET_RE.finditer(html):
            magnet = unescape(match.group(1))  # decode &amp; in tracker params
            title = _title_from_magnet(magnet)
            seeders, leechers, size = stats.get(match.group(1), (0, 0, None))
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                TorrentRelease(
                    source=self.name,
                    title=title,
                    seeders=seeders,
                    leechers=leechers,
                    size_bytes=size,
                    magnet=magnet,
                )
            )
        if not out:
            log.info("torrent.rarbg.no_results", query=query)
        return out


def _size_bytes(text: str) -> int | None:
    match = _SIZE_RE.match(text)
    if match is None:
        return None
    return int(float(match.group(1).replace(",", "")) * _SIZE_UNITS[match.group(2).upper()])


def _row_stats(html: str) -> dict[str, tuple[int, int, int | None]]:
    """Raw magnet href -> (seeders, leechers, size), for rows that show them."""

    stats: dict[str, tuple[int, int, int | None]] = {}
    for row in _ROW_RE.findall(html):
        magnet = _MAGNET_RE.search(row)
        if magnet is None:
            continue
        cells = [_TAG_RE.sub("", c).strip() for c in _CELL_RE.findall(row)]
        for i, cell in enumerate(cells[:-2]):
            size = _size_bytes(cell)
            if size is not None and cells[i + 1].isdigit() and cells[i + 2].isdigit():
                stats[magnet.group(1)] = (int(cells[i + 1]), int(cells[i + 2]), size)
                break
    return stats


def _title_from_magnet(magnet: str) -> str:
    match = _DN_RE.search(magnet)
    if not match:
        return "unknown"
    from urllib.parse import unquote_plus

    return unquote_plus(match.group(1))
