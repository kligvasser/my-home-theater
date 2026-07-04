"""The Pirate Bay via the apibay.org JSON API.

apibay is TPB's own backing API — a plain JSON endpoint, no HTML scraping and no
Cloudflare wall — which makes it the reliable anchor among the sources. A search
with no hits returns a single sentinel row (id "0", name "No results returned").
"""

from __future__ import annotations

import httpx

from ....db.models import TitleKind
from ....logging_setup import get_logger
from ..base import TorrentRelease

log = get_logger(__name__)

_NO_RESULTS_HASH = "0000000000000000000000000000000000000000"


class PirateBaySource:
    name = "piratebay"

    def __init__(self, api_url: str, client: httpx.AsyncClient, *, timeout: float) -> None:
        self._base = api_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    async def search(self, query: str, kind: TitleKind) -> list[TorrentRelease]:
        resp = await self._client.get(
            f"{self._base}/q.php",
            params={"q": query, "cat": ""},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            return []
        out: list[TorrentRelease] = []
        for row in rows:
            info_hash = str(row.get("info_hash", "")).lower()
            if not info_hash or info_hash == _NO_RESULTS_HASH:
                continue
            out.append(
                TorrentRelease(
                    source=self.name,
                    title=str(row.get("name", "")),
                    seeders=_int(row.get("seeders")),
                    leechers=_int(row.get("leechers")),
                    size_bytes=_int(row.get("size")) or None,
                    infohash=info_hash,
                )
            )
        return out


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
