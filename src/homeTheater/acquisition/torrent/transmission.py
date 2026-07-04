"""Transmission RPC download client.

Transmission guards its RPC with a CSRF token: the first call returns 409 with an
``X-Transmission-Session-Id`` header we must echo on every subsequent call (and
refresh whenever a later 409 says it rotated). Auth is optional HTTP Basic.

Docs: https://github.com/transmission/transmission/blob/main/docs/rpc-spec.md
"""

from __future__ import annotations

from typing import Any

import httpx

from ...logging_setup import get_logger
from .base import AddedTorrent, TorrentStatus

log = get_logger(__name__)

_SESSION_HEADER = "X-Transmission-Session-Id"

# torrent-get "status" enum (rpc-spec §3.2). 6 == seeding, i.e. done downloading.
_STATUS_DOWNLOAD_WAIT = 3
_STATUS_DOWNLOADING = 4

_FIELDS = [
    "hashString", "name", "percentDone", "status", "downloadDir", "error", "errorString",
    "rateDownload", "peersSendingToUs", "eta",
]

# torrent id lookups accept the hash string directly (rpc-spec §3.1).


class TransmissionClient:
    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        *,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._url = url
        self._client = client
        self._auth: tuple[str, str] | None = (
            (username, password or "") if username is not None else None
        )
        self._timeout = timeout
        self._session_id: str | None = None

    async def _rpc(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {"method": method, "arguments": arguments}
        for attempt in range(2):  # one retry to pick up a rotated session id
            headers = {_SESSION_HEADER: self._session_id} if self._session_id else {}
            resp = await self._client.post(
                self._url,
                json=payload,
                headers=headers,
                auth=self._auth if self._auth is not None else httpx.USE_CLIENT_DEFAULT,
                timeout=self._timeout,
            )
            if resp.status_code == 409 and attempt == 0:
                self._session_id = resp.headers.get(_SESSION_HEADER)
                continue
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            if data.get("result") != "success":
                raise RuntimeError(f"Transmission RPC {method} failed: {data.get('result')}")
            args: dict[str, Any] = data.get("arguments", {})
            return args
        raise RuntimeError("Transmission RPC failed to negotiate a session id")

    async def add_magnet(self, magnet: str, *, download_dir: str | None) -> AddedTorrent:
        arguments: dict[str, Any] = {"filename": magnet, "paused": False}
        if download_dir:
            arguments["download-dir"] = download_dir
        args = await self._rpc("torrent-add", arguments)
        # Transmission returns torrent-added, or torrent-duplicate if the hash is
        # already present; both carry hashString/name.
        info = args.get("torrent-added") or args.get("torrent-duplicate") or {}
        infohash = str(info.get("hashString", "")).lower()
        if not infohash:
            raise RuntimeError("Transmission accepted the magnet but returned no hash")
        return AddedTorrent(
            infohash=infohash,
            name=str(info.get("name", "")),
            already_existed="torrent-duplicate" in args,
        )

    async def status(self, infohash: str) -> TorrentStatus | None:
        args = await self._rpc("torrent-get", {"ids": [infohash], "fields": _FIELDS})
        torrents = args.get("torrents") or []
        if not torrents:
            return None
        t = torrents[0]
        progress = float(t.get("percentDone", 0.0) or 0.0)
        state = int(t.get("status", 0) or 0)
        complete = progress >= 1.0
        downloading = not complete and (
            state in (_STATUS_DOWNLOAD_WAIT, _STATUS_DOWNLOADING) or progress > 0.0
        )
        error = t.get("errorString") or None if t.get("error") else None
        eta = t.get("eta")
        return TorrentStatus(
            infohash=str(t.get("hashString", infohash)).lower(),
            progress=progress,
            downloading=downloading,
            complete=complete,
            save_path=t.get("downloadDir") or None,
            name=t.get("name") or None,
            error=error,
            down_rate=int(t.get("rateDownload", 0) or 0),
            seeders=int(t.get("peersSendingToUs", 0) or 0),
            eta_seconds=int(eta) if isinstance(eta, int | float) and eta >= 0 else None,
        )

    async def remove(self, infohash: str, *, delete_data: bool) -> None:
        await self._rpc("torrent-remove", {"ids": [infohash], "delete-local-data": delete_data})

    async def set_location(self, infohash: str, location: str, *, move: bool = True) -> None:
        """Move a torrent's data to ``location`` (Transmission does the move, so it
        works even when our process can't read the current, protected dir)."""

        await self._rpc(
            "torrent-set-location", {"ids": [infohash], "location": location, "move": move}
        )
