"""Kodi over JSON-RPC: ask for a library scan after imports, read watch history.

Kodi being off is normal. Nothing here may fail an import, and an unreachable Kodi is
a skip with a note, not a job failure.

Scans are debounced on the trailing edge. An import only leaves a timestamp in the
``setting`` table; the ``kodi_scan`` job sends one ``VideoLibrary.Scan`` once
``scan_debounce_seconds`` have passed without another, and holds off while an import
is copying, since a scan reads the same NAS. A season imported in several sync rounds
is one scan, and a request made while Kodi is off is still there when it comes back.

Kodi reports ``smb://`` paths; the catalog holds UNC paths. ``kodi.path_map`` says how
one becomes the other. A path it doesn't cover, or that lands on no owned file, is
reported and left out. Paths are never fuzzy-matched: a wrong match would put someone
else's history on a file.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select

from .config import AppConfig
from .db.base import utcnow
from .db.models import OwnedFile, Setting
from .db.session import session_scope
from .errors import NotConfiguredError, redact, redact_exc
from .locks import is_import_active, job_lock
from .logging_setup import get_logger
from .watch import KODI_STATUS_KEY, SOURCE_KODI, merge_watch, save_status

log = get_logger(__name__)

SCAN_WANTED_KEY = "kodi_scan_wanted_at"
HISTORY_LOCK = "kodi-sync"
NOTE_TIMEOUT_S = 5.0  # an import never waits longer than this on the hook
PAGE_SIZE = 500
MAX_UNMAPPED_SHOWN = 50
_WATCH_PROPERTIES = ["playcount", "lastplayed", "file"]
_KODI_TIME = "%Y-%m-%d %H:%M:%S"
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s]*@")


class KodiUnavailableError(RuntimeError):
    """Kodi can't be asked right now. Routine: it may simply be off."""


@dataclass
class KodiSyncStats:
    seen: int = 0
    mapped: int = 0
    updated: int = 0
    unmapped: int = 0
    unmapped_paths: list[dict[str, str]] = field(default_factory=list)  # capped sample

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def add_unmapped(self, path: str, reason: str) -> None:
        self.unmapped += 1
        if len(self.unmapped_paths) < MAX_UNMAPPED_SHOWN:
            self.unmapped_paths.append({"path": redact(path), "reason": reason})


class KodiClient:
    def __init__(self, url: str, http: httpx.AsyncClient) -> None:
        self._url = url
        self._http = http

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            body["params"] = params
        try:
            resp = await self._http.post(self._url, json=body)
        except httpx.TransportError as exc:
            log.info("kodi.unreachable", detail=redact_exc(exc))
            raise KodiUnavailableError("Kodi is not reachable") from None
        if resp.status_code in (401, 403):
            raise NotConfiguredError("Kodi refused the login; check KODI_USER and KODI_PASS.")
        if not resp.is_success:
            raise KodiUnavailableError(f"Kodi answered HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise KodiUnavailableError("Kodi's answer was not JSON") from None
        error = data.get("error") if isinstance(data, dict) else "malformed"
        if error:
            code = error.get("code") if isinstance(error, dict) else error
            raise KodiUnavailableError(f"Kodi refused {method} ({code})")
        return data.get("result")

    async def scan(self) -> None:
        await self.call("VideoLibrary.Scan")

    async def _all(self, method: str, key: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            result = await self.call(
                method,
                {
                    "properties": _WATCH_PROPERTIES,
                    "limits": {"start": len(out), "end": len(out) + PAGE_SIZE},
                },
            )
            page = (result or {}).get(key) or []
            out += page
            total = ((result or {}).get("limits") or {}).get("total") or 0
            if not page or len(out) >= total:
                return out

    async def watch_history(self) -> list[dict[str, Any]]:
        return [
            *await self._all("VideoLibrary.GetMovies", "movies"),
            *await self._all("VideoLibrary.GetEpisodes", "episodes"),
        ]


def _client(config: AppConfig, http: httpx.AsyncClient) -> KodiClient:
    if not config.kodi.url:
        raise NotConfiguredError("kodi.url is not set.")
    return KodiClient(config.kodi.url, http)


def _http(config: AppConfig) -> httpx.AsyncClient:
    user = config.secrets.kodi_user
    password = config.secrets.kodi_pass
    auth = httpx.BasicAuth(user, password.get_secret_value() if password else "") if user else None
    return httpx.AsyncClient(timeout=config.kodi.request_timeout, auth=auth)


def strip_userinfo(path: str) -> str:
    """Kodi keeps source logins inside its paths: smb://user:pass@host/share/..."""

    return _USERINFO_RE.sub(r"\1", path)


def map_kodi_path(path: str, path_map: dict[str, str]) -> str | None:
    """Catalog path for a Kodi path by longest prefix, or None if none covers it."""

    for kodi_prefix in sorted(path_map, key=len, reverse=True):
        prefix = kodi_prefix.rstrip("/") + "/"
        if path.startswith(prefix):
            target = path_map[kodi_prefix]
            separator = "\\" if target.startswith("\\\\") else "/"
            rest = path[len(prefix) :].replace("/", separator)
            return target.rstrip("\\/") + separator + rest
    return None


def parse_kodi_time(value: str | None, timezone: str | None) -> datetime | None:
    """UTC time of a ``lastplayed``, which Kodi gives in its own local time."""

    if not value:
        return None
    try:
        naive = datetime.strptime(value, _KODI_TIME)  # noqa: DTZ007 - no zone in the data
    except ValueError:
        return None
    if timezone:
        return naive.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    return naive.astimezone().astimezone(UTC)  # naive means host-local here


def _catalog_lookup(catalog: dict[str, int], path: str) -> int | None:
    for variant in (path, unquote(path)):
        for form in (variant, unicodedata.normalize("NFC", variant)):
            if form in catalog:
                return catalog[form]
    return None


def _apply_history(config: AppConfig, items: list[dict[str, Any]], stats: KodiSyncStats) -> None:
    timezone = config.kodi.timezone
    now = utcnow()
    with session_scope() as s:
        catalog: dict[str, int] = {}
        for owned_id, path in s.execute(select(OwnedFile.id, OwnedFile.path)):
            catalog[path] = owned_id
            catalog.setdefault(unicodedata.normalize("NFC", path), owned_id)
        for item in items:
            stats.seen += 1
            raw = str(item.get("file") or "")
            if not raw:
                continue
            path = strip_userinfo(raw)
            mapped = map_kodi_path(path, config.kodi.path_map)
            if mapped is None:
                stats.add_unmapped(path, "no kodi.path_map prefix covers it")
                continue
            owned_id = _catalog_lookup(catalog, mapped)
            if owned_id is None:
                stats.add_unmapped(path, "not in the catalog after mapping")
                continue
            stats.mapped += 1
            plays = int(item.get("playcount") or 0)
            played = parse_kodi_time(item.get("lastplayed"), timezone)
            if merge_watch(s, owned_id, SOURCE_KODI, plays, played, now):
                stats.updated += 1


def _require_enabled(config: AppConfig) -> None:
    if not config.kodi.enabled:
        raise NotConfiguredError("kodi.enabled is off.")
    if config.kodi.timezone:
        try:
            ZoneInfo(config.kodi.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise NotConfiguredError(
                f"kodi.timezone {config.kodi.timezone!r} is unknown."
            ) from None


async def sync_history(config: AppConfig) -> KodiSyncStats:
    """Read Kodi's watch history into ``watch_state``.

    Raises KodiUnavailableError when Kodi can't be asked; nothing changes then.
    """

    _require_enabled(config)
    if not config.kodi.path_map:
        raise NotConfiguredError("kodi.path_map is empty, so no Kodi path can be matched.")
    stats = KodiSyncStats()
    with job_lock(config, HISTORY_LOCK):
        async with _http(config) as http:
            items = await _client(config, http).watch_history()
        await asyncio.to_thread(_apply_history, config, items, stats)
        await asyncio.to_thread(save_status, KODI_STATUS_KEY, stats.as_dict())
    if stats.unmapped:
        log.warning("kodi.unmapped_paths", count=stats.unmapped)
    log.info("kodi.history_done", seen=stats.seen, updated=stats.updated, unmapped=stats.unmapped)
    return stats


def _mark_scan_wanted() -> None:
    with session_scope() as s:
        row = s.get(Setting, SCAN_WANTED_KEY)
        if row is None:
            row = Setting(key=SCAN_WANTED_KEY)
            s.add(row)
        row.value = json.dumps(utcnow().isoformat())


async def note_import(config: AppConfig) -> None:
    """Called after an import. Cheap, bounded, and it cannot raise an ordinary error."""

    if not config.kodi.enabled:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(_mark_scan_wanted), NOTE_TIMEOUT_S)
    except Exception as exc:
        log.info("kodi.scan_not_noted", detail=redact_exc(exc))


def _scan_wanted_at() -> tuple[str, datetime] | None:
    with session_scope() as s:
        row = s.get(Setting, SCAN_WANTED_KEY)
        if row is None or not row.value:
            return None
        return row.value, datetime.fromisoformat(json.loads(row.value))


def _clear_scan_wanted(seen: str) -> None:
    """Only the request that was answered: one made during the call still stands."""

    with session_scope() as s:
        row = s.get(Setting, SCAN_WANTED_KEY)
        if row is not None and row.value == seen:
            s.delete(row)


async def scan_if_due(config: AppConfig) -> bool:
    """Send the debounced scan if one is due. True if Kodi accepted it."""

    _require_enabled(config)
    wanted = await asyncio.to_thread(_scan_wanted_at)
    if wanted is None:
        return False
    raw, wanted_at = wanted
    if utcnow() - wanted_at < timedelta(seconds=config.kodi.scan_debounce_seconds):
        return False
    if await asyncio.to_thread(is_import_active, config):
        return False
    async with _http(config) as http:
        await _client(config, http).scan()
    await asyncio.to_thread(_clear_scan_wanted, raw)
    log.info("kodi.scan_requested")
    return True


__all__ = [
    "KodiClient",
    "KodiSyncStats",
    "KodiUnavailableError",
    "map_kodi_path",
    "note_import",
    "parse_kodi_time",
    "scan_if_due",
    "strip_userinfo",
    "sync_history",
]
