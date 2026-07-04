"""Trakt client: device-code OAuth + watchlist (plan §3 'watchlist source').

Auth is the interactive *device flow* (``home-theater trakt-auth``): Trakt shows
a short code, you approve it at trakt.tv/activate, and the resulting tokens are
stored in the ``setting`` table (auto-refreshed on expiry). The watchlist then
becomes a first-class discovery source — items you explicitly picked bypass the
rating/vote thresholds.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .db.models import Setting, TitleKind
from .db.session import session_scope
from .errors import NotConfiguredError, redact_exc
from .logging_setup import get_logger

log = get_logger(__name__)

BASE_URL = "https://api.trakt.tv"
TOKEN_KEY = "trakt_token"
# Refresh a little early so a token never expires mid-run.
EXPIRY_SLACK_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class WatchlistItem:
    kind: TitleKind
    title: str
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None


def _headers(client_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    }


def load_token() -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.get(Setting, TOKEN_KEY)
        if row is None or not row.value:
            return None
        try:
            data = json.loads(row.value)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


def save_token(payload: dict[str, Any]) -> None:
    payload = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": int(time.time()) + int(payload.get("expires_in") or 7776000),
    }
    with session_scope() as s:
        row = s.get(Setting, TOKEN_KEY)
        if row is None:
            row = Setting(key=TOKEN_KEY)
            s.add(row)
        row.value = json.dumps(payload)


class TraktClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient) -> None:
        self._id = client_id
        self._secret = client_secret
        self._http = http

    # -- device-code auth -------------------------------------------------
    async def device_code(self) -> dict[str, Any]:
        resp = await self._http.post(
            f"{BASE_URL}/oauth/device/code",
            json={"client_id": self._id},
            headers=_headers(self._id),
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def poll_device_token(self, device: dict[str, Any]) -> dict[str, Any]:
        """Poll until the user approves the code (or it expires)."""

        interval = int(device.get("interval") or 5)
        deadline = time.time() + int(device.get("expires_in") or 600)
        while time.time() < deadline:
            resp = await self._http.post(
                f"{BASE_URL}/oauth/device/token",
                json={
                    "code": device["device_code"],
                    "client_id": self._id,
                    "client_secret": self._secret,
                },
                headers=_headers(self._id),
            )
            if resp.status_code == 200:
                token: dict[str, Any] = resp.json()
                save_token(token)
                return token
            if resp.status_code not in (400, 429):  # 400 = still pending
                resp.raise_for_status()
            await asyncio.sleep(interval)
        raise TimeoutError("Trakt device code expired before it was approved.")

    async def _refresh(self, refresh_token: str) -> dict[str, Any]:
        resp = await self._http.post(
            f"{BASE_URL}/oauth/token",
            json={
                "refresh_token": refresh_token,
                "client_id": self._id,
                "client_secret": self._secret,
                "grant_type": "refresh_token",
            },
            headers=_headers(self._id),
        )
        resp.raise_for_status()
        token: dict[str, Any] = resp.json()
        save_token(token)
        return token

    async def _access_token(self) -> str:
        token = load_token()
        if token is None:
            raise NotConfiguredError(
                "Trakt is not authorized yet — run `home-theater trakt-auth` first."
            )
        if token.get("expires_at", 0) < time.time() + EXPIRY_SLACK_SECONDS and token.get(
            "refresh_token"
        ):
            try:
                token = await self._refresh(token["refresh_token"])
            except httpx.HTTPError as exc:
                log.warning("trakt.refresh_failed", error=redact_exc(exc))
        return str(token["access_token"])

    # -- watchlist ---------------------------------------------------------
    async def watchlist(self) -> list[WatchlistItem]:
        access = await self._access_token()
        headers = {**_headers(self._id), "Authorization": f"Bearer {access}"}
        out: list[WatchlistItem] = []
        for path, node, kind in (
            ("/sync/watchlist/movies", "movie", TitleKind.movie),
            ("/sync/watchlist/shows", "show", TitleKind.series),
        ):
            resp = await self._http.get(f"{BASE_URL}{path}", headers=headers)
            resp.raise_for_status()
            for entry in resp.json():
                media = entry.get(node) or {}
                ids = media.get("ids") or {}
                out.append(
                    WatchlistItem(
                        kind=kind,
                        title=media.get("title") or "",
                        year=media.get("year"),
                        tmdb_id=ids.get("tmdb"),
                        imdb_id=ids.get("imdb"),
                    )
                )
        return out
