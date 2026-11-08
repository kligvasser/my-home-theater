"""Trakt client: device-code OAuth + watchlist (plan §3 'watchlist source').

Auth is the interactive *device flow* (``home-theater trakt-auth``): Trakt shows
a short code, you approve it at trakt.tv/activate, and the resulting tokens are
stored in the ``setting`` table (auto-refreshed on expiry). The watchlist then
becomes a first-class discovery source — items you explicitly picked bypass the
rating/vote thresholds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
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
_REAUTHORIZE = "Trakt authorization has expired — run `home-theater trakt-auth` again."
WATCHLIST_PAGE_SIZE = 100
MAX_WATCHLIST_PAGES = 50
MAX_RATE_LIMIT_RETRIES = 3
RETRY_AFTER_CAP_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class WatchlistItem:
    kind: TitleKind
    title: str
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None


@dataclass(frozen=True, slots=True)
class WatchedMovie:
    tmdb_id: int | None
    imdb_id: str | None
    plays: int
    last_watched_at: datetime | None


@dataclass(frozen=True, slots=True)
class WatchedEpisode:
    show_tmdb_id: int | None
    show_imdb_id: str | None
    season: int
    episode: int
    plays: int
    last_watched_at: datetime | None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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


def _retry_delay(resp: httpx.Response) -> float:
    delay = 1.0
    with contextlib.suppress(ValueError):
        delay = float(resp.headers.get("Retry-After", delay))
    log.info("trakt.rate_limited", delay=delay)
    return min(delay, RETRY_AFTER_CAP_SECONDS)


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
        # Trakt rotates the refresh token, but a reply without one must not lose it.
        token.setdefault("refresh_token", refresh_token)
        save_token(token)
        return token

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        """A usable access token, refreshed when it's near expiry.

        A failed refresh is only survivable while the old token still works. Past
        that, a rejected refresh token means re-authorizing, so it reads as "not
        configured" rather than failing every run with a 401.
        """

        token = load_token()
        if token is None or not token.get("access_token"):
            raise NotConfiguredError(
                "Trakt is not authorized yet — run `home-theater trakt-auth` first."
            )
        expires_at = token.get("expires_at", 0)
        expired = force_refresh or expires_at < time.time()
        refresh_token = token.get("refresh_token")
        if force_refresh or expires_at < time.time() + EXPIRY_SLACK_SECONDS:
            if not refresh_token:
                if expired:
                    raise NotConfiguredError(_REAUTHORIZE)
                return str(token["access_token"])
            try:
                token = await self._refresh(refresh_token)
            except httpx.HTTPStatusError as exc:
                log.warning("trakt.refresh_failed", error=redact_exc(exc))
                if expired:
                    raise NotConfiguredError(_REAUTHORIZE) from None
            except httpx.TransportError as exc:
                log.warning("trakt.refresh_failed", error=redact_exc(exc))
                if expired:
                    raise
        return str(token["access_token"])

    async def _get_page(self, path: str, access: str, page: int) -> httpx.Response:
        """One watchlist page, waiting out rate limits. 401 is left to the caller."""

        headers = {**_headers(self._id), "Authorization": f"Bearer {access}"}
        params = {"page": page, "limit": WATCHLIST_PAGE_SIZE}
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            resp = await self._http.get(f"{BASE_URL}{path}", headers=headers, params=params)
            if resp.status_code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                break
            await asyncio.sleep(_retry_delay(resp))
        return resp

    # -- watchlist ---------------------------------------------------------
    async def watchlist(self) -> list[WatchlistItem]:
        access = await self._access_token()
        out: list[WatchlistItem] = []
        for path, node, kind in (
            ("/sync/watchlist/movies", "movie", TitleKind.movie),
            ("/sync/watchlist/shows", "show", TitleKind.series),
        ):
            for page in range(1, MAX_WATCHLIST_PAGES + 1):
                resp = await self._get_page(path, access, page)
                if resp.status_code == 401:  # revoked early: expires_at can't tell us
                    access = await self._access_token(force_refresh=True)
                    resp = await self._get_page(path, access, page)
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
                if page >= int(resp.headers.get("X-Pagination-Page-Count") or 1):
                    break
        return out

    # -- watched history ---------------------------------------------------
    async def _all_pages(self, path: str) -> list[dict[str, Any]]:
        access = await self._access_token()
        out: list[dict[str, Any]] = []
        for page in range(1, MAX_WATCHLIST_PAGES + 1):
            resp = await self._get_page(path, access, page)
            if resp.status_code == 401:
                access = await self._access_token(force_refresh=True)
                resp = await self._get_page(path, access, page)
            resp.raise_for_status()
            out += resp.json()
            if page >= int(resp.headers.get("X-Pagination-Page-Count") or 1):
                break
        return out

    async def watched(self) -> tuple[list[WatchedMovie], list[WatchedEpisode]]:
        """Everything Trakt has as watched. Both lists are whole or this raises, so a
        failed page can't read as "not watched"."""

        movies: list[WatchedMovie] = []
        for entry in await self._all_pages("/sync/watched/movies"):
            ids = (entry.get("movie") or {}).get("ids") or {}
            movies.append(
                WatchedMovie(
                    tmdb_id=ids.get("tmdb"),
                    imdb_id=ids.get("imdb"),
                    plays=int(entry.get("plays") or 1),
                    last_watched_at=_parse_time(entry.get("last_watched_at")),
                )
            )
        episodes: list[WatchedEpisode] = []
        for entry in await self._all_pages("/sync/watched/shows"):
            ids = (entry.get("show") or {}).get("ids") or {}
            for season in entry.get("seasons") or []:
                for ep in season.get("episodes") or []:
                    episodes.append(
                        WatchedEpisode(
                            show_tmdb_id=ids.get("tmdb"),
                            show_imdb_id=ids.get("imdb"),
                            season=int(season["number"]),
                            episode=int(ep["number"]),
                            plays=int(ep.get("plays") or 1),
                            last_watched_at=_parse_time(ep.get("last_watched_at")),
                        )
                    )
        return movies, episodes

    async def _post_history(self, payload: dict[str, Any], access: str) -> httpx.Response:
        headers = {**_headers(self._id), "Authorization": f"Bearer {access}"}
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            resp = await self._http.post(f"{BASE_URL}/sync/history", headers=headers, json=payload)
            if resp.status_code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                break
            await asyncio.sleep(_retry_delay(resp))
        return resp

    async def add_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /sync/history``. Trakt adds what it's given, so deciding what's safe
        to send is the caller's job (see :mod:`homeTheater.watch`)."""

        access = await self._access_token()
        resp = await self._post_history(payload, access)
        if resp.status_code == 401:
            access = await self._access_token(force_refresh=True)
            resp = await self._post_history(payload, access)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
