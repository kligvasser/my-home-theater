"""Interfaces + DTOs for native torrent acquisition.

The torrent backend is two clean interfaces so a site/API change (or swapping the
download client) touches one file (plan §12):

* :class:`TorrentSource` — search an indexer, return candidate releases.
* :class:`DownloadClient` — add a magnet and report its progress.

Everything the rest of the app sees is a :class:`TorrentRelease` /
:class:`TorrentStatus`, never a site's raw payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from ...db.models import TitleKind

# A public tracker set appended to bare-infohash magnets (apibay returns only the
# hash). These are the trackers apibay's own web UI uses; more trackers = more
# peer sources, they don't affect *what* is downloaded.
DEFAULT_TRACKERS: tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
)


@dataclass(frozen=True, slots=True)
class TorrentRelease:
    """One search hit from an indexer."""

    source: str  # indexer name ("piratebay", "1337x", ...)
    title: str  # the release name (used for resolution/quality parsing)
    seeders: int
    leechers: int
    size_bytes: int | None
    infohash: str | None = None  # 40-hex btih; enough to build a magnet
    magnet: str | None = None  # some sources hand back a full magnet directly

    def magnet_uri(self) -> str | None:
        """A usable magnet, built from the infohash if the source gave only that."""

        if self.magnet:
            return self.magnet
        if not self.infohash:
            return None
        trackers = "".join(f"&tr={quote(t, safe='')}" for t in DEFAULT_TRACKERS)
        return f"magnet:?xt=urn:btih:{self.infohash}&dn={quote(self.title)}{trackers}"


class TorrentSource(Protocol):
    """A searchable indexer. Implementations must never raise for 'no results' —
    return an empty list; raise only on real transport/parse failures."""

    name: str

    async def search(self, query: str, kind: TitleKind) -> list[TorrentRelease]: ...


@dataclass(frozen=True, slots=True)
class AddedTorrent:
    infohash: str  # lower-case btih; the handle we persist as Download.external_id
    name: str
    already_existed: bool = False


@dataclass(frozen=True, slots=True)
class TorrentStatus:
    infohash: str
    progress: float  # 0.0 .. 1.0
    downloading: bool
    complete: bool
    save_path: str | None  # the client's download dir for this torrent
    name: str | None = None  # torrent name (file or top-level folder)
    error: str | None = None
    # Live transfer stats for the dashboard (best-effort; None when unknown).
    down_rate: int | None = None  # bytes/sec
    seeders: int | None = None  # peers sending to us
    eta_seconds: int | None = None  # client estimate; negative/None = unknown

    def content_path(self) -> str | None:
        """Local path to the downloaded file/folder (``save_path`` joined to
        ``name``), or ``None`` if the client didn't report both."""

        if not self.save_path or not self.name:
            return None
        return f"{self.save_path.rstrip('/')}/{self.name}"


class DownloadClient(Protocol):
    """A torrent download client (Transmission today)."""

    async def add_magnet(self, magnet: str, *, download_dir: str | None) -> AddedTorrent: ...

    async def status(self, infohash: str) -> TorrentStatus | None:
        """Current state, or ``None`` if the client no longer knows this hash."""
        ...

    async def remove(self, infohash: str, *, delete_data: bool) -> None:
        """Remove a torrent, optionally deleting its downloaded files."""
        ...
