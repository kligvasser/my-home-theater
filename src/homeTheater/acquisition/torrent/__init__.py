"""Native torrent acquisition backend (acquisition.backend == 'torrent').

Search indexers directly and push the chosen magnet to a torrent client,
bypassing the Radarr/Sonarr stack. Kept behind a config switch; the arr path
stays the default. See :mod:`homeTheater.acquisition.torrent.service`.
"""

from .base import (
    AddedTorrent,
    DownloadClient,
    TorrentRelease,
    TorrentSource,
    TorrentStatus,
)
from .service import queue_candidate_torrent, sync_downloads_torrent

__all__ = [
    "AddedTorrent",
    "DownloadClient",
    "TorrentRelease",
    "TorrentSource",
    "TorrentStatus",
    "queue_candidate_torrent",
    "sync_downloads_torrent",
]
