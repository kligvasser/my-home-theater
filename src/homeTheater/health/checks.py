"""Provider health checks (plan §5.11: surface provider status on the dashboard).

Each check is best-effort and never raises: a provider that isn't configured
reports ``configured=False``; a configured-but-unreachable one reports
``ok=False`` with the error. Used by the status page + JSON API.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from ..config import AppConfig
from ..errors import redact_exc

_TIMEOUT = httpx.Timeout(6.0)

# Results are cached briefly so the (unauthenticated) status page can't be used
# to hammer providers or burn API quota on every page load.
CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    name: str
    configured: bool
    ok: bool | None  # None = configured but not actively probed (e.g. SMB)
    detail: str = ""


async def _probe(
    name: str,
    url: str,
    params: dict[str, str] | None,
    headers: dict[str, str] | None,
) -> ProviderStatus:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            resp = await http.get(url, params=params or {}, headers=headers or {})
            resp.raise_for_status()
        return ProviderStatus(name, True, True, "ok")
    except Exception as exc:
        # Never store/display the raw error: httpx messages embed the full
        # request URL, query-string credentials included.
        return ProviderStatus(name, True, False, redact_exc(exc))


async def check_tmdb(config: AppConfig) -> ProviderStatus:
    key = config.secrets.tmdb_api_key
    if key is None:
        return ProviderStatus("tmdb", False, None)
    return await _probe(
        "tmdb",
        "https://api.themoviedb.org/3/configuration",
        {"api_key": key.get_secret_value()},
        None,
    )


async def check_omdb(config: AppConfig) -> ProviderStatus:
    key = config.secrets.omdb_api_key
    if key is None:
        return ProviderStatus("omdb", False, None)
    return await _probe(
        "omdb",
        "https://www.omdbapi.com/",
        {"apikey": key.get_secret_value(), "i": "tt0133093"},
        None,
    )


async def _check_arr(name: str, url: str | None, key: SecretStr | None) -> ProviderStatus:
    if not (url and key):
        return ProviderStatus(name, False, None)
    return await _probe(
        name,
        f"{url.rstrip('/')}/api/v3/system/status",
        None,
        {"X-Api-Key": key.get_secret_value()},
    )


async def check_radarr(config: AppConfig) -> ProviderStatus:
    return await _check_arr("radarr", config.secrets.radarr_url, config.secrets.radarr_api_key)


async def check_sonarr(config: AppConfig) -> ProviderStatus:
    return await _check_arr("sonarr", config.secrets.sonarr_url, config.secrets.sonarr_api_key)


async def check_bazarr(config: AppConfig) -> ProviderStatus:
    url, key = config.secrets.bazarr_url, config.secrets.bazarr_api_key
    if not (url and key):
        return ProviderStatus("bazarr", False, None)
    return await _probe(
        "bazarr",
        f"{url.rstrip('/')}/api/system/status",
        None,
        {"X-API-KEY": key.get_secret_value()},
    )


async def check_smb(config: AppConfig) -> ProviderStatus:
    # A real SMB connect is heavy; just report configuration state.
    configured = bool(config.secrets.smb_host and config.secrets.smb_user)
    return ProviderStatus("smb", configured, None, "configured" if configured else "not set")


_cache: tuple[float, list[ProviderStatus]] | None = None
_cache_lock = asyncio.Lock()


def clear_cache() -> None:
    """Drop cached results (tests / forced refresh)."""

    global _cache
    _cache = None


async def check_all(config: AppConfig) -> list[ProviderStatus]:
    """Probe all providers, serving results from a short-lived cache."""

    global _cache
    async with _cache_lock:
        now = time.monotonic()
        if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
            return _cache[1]
        statuses = list(
            await asyncio.gather(
                check_tmdb(config),
                check_omdb(config),
                check_radarr(config),
                check_sonarr(config),
                check_bazarr(config),
                check_smb(config),
            )
        )
        _cache = (now, statuses)
        return statuses
