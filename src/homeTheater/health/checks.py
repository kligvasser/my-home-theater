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


async def check_transmission(config: AppConfig) -> ProviderStatus:
    """Torrent download client: version + how many torrents it's holding."""

    if config.acquisition.backend != "torrent":
        return ProviderStatus("transmission", False, None, "acquisition backend is 'arr'")
    from typing import cast

    from ..acquisition.torrent.service import _download_client
    from ..acquisition.torrent.transmission import TransmissionClient

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            client = cast(TransmissionClient, _download_client(config, http))
            version, n = await client.ping()
            return ProviderStatus("transmission", True, True, f"v{version} · {n} active torrent(s)")
    except Exception as exc:
        return ProviderStatus("transmission", True, False, redact_exc(exc))


async def check_nas_mount(config: AppConfig) -> ProviderStatus:
    """The local library mount used for reliable NAS reads/writes (macOS TCC)."""

    import os

    base = config.torrent.library_base_dir or config.subtitles.library_base_dir
    if not base:
        return ProviderStatus("nas-mount", False, None, "writing via SMB (no local mount set)")

    # Parse the mount table via a cancellable subprocess rather than stat()ing the
    # path: a stat on a WEDGED SMB mount hangs forever and would leak a threadpool
    # worker (eventually starving every asyncio.to_thread call server-wide).
    try:
        proc = await asyncio.create_subprocess_exec(
            "mount", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
    except Exception:
        return ProviderStatus("nas-mount", True, None, f"{base} (mount state unknown)")
    if f" on {base} ".encode() in out:  # "…share on /Volumes/X (smbfs, …)"
        return ProviderStatus("nas-mount", True, True, f"mounted at {base}")
    # Not a mount point -> safe to stat (only wedged mounts hang); a plain local dir
    # is a perfectly valid library target.
    if os.path.isdir(base):
        return ProviderStatus("nas-mount", True, True, f"{base} (local dir)")
    return ProviderStatus("nas-mount", True, False, f"{base} NOT mounted")


async def check_opensubtitles(config: AppConfig) -> ProviderStatus:
    """OpenSubtitles.com: reachability + today's remaining download quota."""

    if config.subtitles.backend != "native":
        return ProviderStatus("opensubtitles.com", False, None, "subtitles backend is 'bazarr'")
    s = config.secrets
    if s.opensubtitles_api_key is None:
        return ProviderStatus("opensubtitles.com", False, None, "no API key")
    headers = {
        "Api-Key": s.opensubtitles_api_key.get_secret_value(),
        "User-Agent": config.subtitles.opensubtitles_user_agent,
        "Content-Type": "application/json",
    }
    base = "https://api.opensubtitles.com/api/v1"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            if not (s.opensubtitles_username and s.opensubtitles_password):
                (await http.get(f"{base}/infos/formats", headers=headers)).raise_for_status()
                return ProviderStatus("opensubtitles.com", True, True, "search only (no login)")
            login = await http.post(
                f"{base}/login",
                json={
                    "username": s.opensubtitles_username,
                    "password": s.opensubtitles_password.get_secret_value(),
                },
                headers=headers,
            )
            login.raise_for_status()
            token = login.json().get("token")
            info = await http.get(
                f"{base}/infos/user", headers={**headers, "Authorization": f"Bearer {token}"}
            )
            info.raise_for_status()
            d = info.json().get("data", {})
            return ProviderStatus(
                "opensubtitles.com",
                True,
                True,
                f"{d.get('remaining_downloads', '?')}/{d.get('allowed_downloads', '?')} "
                "downloads left today",
            )
    except Exception as exc:
        return ProviderStatus("opensubtitles.com", True, False, redact_exc(exc))


def _configured(name: str, ok: bool, yes: str, no: str) -> ProviderStatus:
    return ProviderStatus(name, ok, None, yes if ok else no)


async def check_subtitle_accounts(config: AppConfig) -> list[ProviderStatus]:
    """Config-state for the other native subtitle sources (login probes are heavy)."""

    if config.subtitles.backend != "native":
        return []
    s = config.secrets
    sources = config.subtitles.sources
    out: list[ProviderStatus] = []
    if "opensubtitles_org" in sources:
        ok = bool(s.opensubtitles_org_username and s.opensubtitles_org_password)
        out.append(_configured("opensubtitles.org", ok, "credentials set", "no credentials"))
    if "ktuvit" in sources:
        ok = bool(s.ktuvit_email and s.ktuvit_password)
        out.append(_configured("ktuvit", ok, "account set (Hebrew)", "no account"))
    return out


_cache: tuple[float, str, list[ProviderStatus]] | None = None
_cache_lock = asyncio.Lock()


def clear_cache() -> None:
    """Drop cached results (tests / forced refresh)."""

    global _cache
    _cache = None


async def check_all(config: AppConfig) -> list[ProviderStatus]:
    """Probe the services relevant to the configured stack (short-lived cache)."""

    global _cache
    # Key the cache on the active backends so a runtime backend flip doesn't serve
    # the previous stack's provider list for a TTL.
    key = f"{config.acquisition.backend}:{config.subtitles.backend}"
    async with _cache_lock:
        now = time.monotonic()
        if _cache is not None and _cache[1] == key and now - _cache[0] < CACHE_TTL_SECONDS:
            return _cache[2]

        checks = [check_tmdb(config), check_omdb(config), check_smb(config)]
        if config.acquisition.backend == "torrent":
            checks += [check_transmission(config), check_nas_mount(config)]
        else:
            checks += [check_radarr(config), check_sonarr(config)]
        if config.subtitles.backend == "native":
            checks.append(check_opensubtitles(config))
        else:
            checks.append(check_bazarr(config))

        statuses = list(await asyncio.gather(*checks))
        statuses += await check_subtitle_accounts(config)
        _cache = (now, key, statuses)
        return statuses
