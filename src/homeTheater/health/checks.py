"""Provider health checks (plan §5.11: surface provider status on the dashboard).

Each check is best-effort and never raises: a provider that isn't configured
reports ``configured=False``; a configured-but-unreachable one reports
``ok=False`` with the error. Used by the status page + JSON API.

Nothing here may touch a NAS path from the app's own process: a ``stat`` or write on
a wedged SMB mount blocks forever, and no timeout can get a thread back from that.
Filesystem probes run in a subprocess that can be killed instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from ..config import AppConfig
from ..errors import redact_exc

_TIMEOUT = httpx.Timeout(5.0)
# Hard cap per probe so one provider that ignores the httpx timeout (slow TLS,
# mDNS, a wedged socket) can't hang the whole Status page.
_PROBE_CAP_SECONDS = 7.0

# Sum stays under the probe cap, so a wedged mount reports "wedged", not "timed out".
_MOUNT_TIMEOUT_SECONDS = 2.0
_WRITE_PROBE_TIMEOUT_SECONDS = 4.0
_KILL_WAIT_SECONDS = 1.0

# argv[1] is the directory. Exit 2: not a directory. Exit 3: can't write there.
_WRITE_PROBE = """
import os, sys
base = sys.argv[1]
if not os.path.isdir(base):
    sys.exit(2)
path = os.path.join(base, ".home-theater-probe-" + os.urandom(8).hex())
try:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, b"probe")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.unlink(path)
except OSError:
    sys.exit(3)
"""
_MOUNT_LINE_RE = re.compile(r"^.+? on (.+) \(", re.MULTILINE)

# An OpenSubtitles login token lasts 24h and logins are rate-limited; keep it well
# inside that so the Status page doesn't log in on every refresh.
_OS_TOKEN_TTL_SECONDS = 12 * 3600
_os_token: tuple[float, str, str] | None = None  # (expires, username, token)

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
    # Same rule as TMDbClient: JWT-shaped is a v4 token, a bare key is a param.
    value = key.get_secret_value()
    bearer = value.count(".") == 2
    return await _probe(
        "tmdb",
        "https://api.themoviedb.org/3/configuration",
        None if bearer else {"api_key": value},
        {"Authorization": f"Bearer {value}"} if bearer else None,
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


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill a probe and reap it, without waiting on it for long: a process stuck in
    a kernel filesystem call can't die until the call returns."""

    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), _KILL_WAIT_SECONDS)


async def _mount_points() -> list[str] | None:
    """Mount points from the ``mount`` table, or None if it can't be read in time."""

    proc = await asyncio.create_subprocess_exec(
        "mount", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), _MOUNT_TIMEOUT_SECONDS)
    except TimeoutError:
        return None
    finally:
        await _kill(proc)
    return _MOUNT_LINE_RE.findall(out.decode("utf-8", "replace"))


async def _write_probe(base: str) -> int | None:
    """Create + fsync + delete a temp file in ``base`` from a killable subprocess.
    Returns its exit status, or None if it had to be killed (a wedged filesystem)."""

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        _WRITE_PROBE,
        base,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(proc.wait(), _WRITE_PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return None
    finally:
        await _kill(proc)


async def _check_library_dir(base: str) -> ProviderStatus:
    name = "nas-mount"
    # Parse the mount table rather than stat()ing the path: see the module docstring.
    try:
        mounts = await _mount_points()
    except OSError:
        mounts = None
    if mounts is None:
        return ProviderStatus(name, True, None, f"{base} (mount state unknown)")

    trimmed = base.rstrip("/") or "/"
    mounted = any(
        m != "/" and (trimmed == m.rstrip("/") or trimmed.startswith(m.rstrip("/") + "/"))
        for m in mounts
    )
    if not mounted and trimmed.startswith("/Volumes/"):
        # A leftover empty folder here is not the share, and a write to it lands on
        # the boot disk. Never probe it.
        return ProviderStatus(name, True, False, f"{base} NOT mounted")

    result = await _write_probe(base)
    if result is None:
        return ProviderStatus(name, True, False, f"{base} is wedged (write probe timed out)")
    if result == 2:
        return ProviderStatus(name, True, False, f"{base} NOT mounted")
    if result != 0:
        return ProviderStatus(name, True, False, f"{base} is not writable")
    return ProviderStatus(
        name, True, True, f"mounted at {base}" if mounted else f"{base} (local dir)"
    )


def _library_dirs(config: AppConfig) -> list[str]:
    """Local library paths the configured backends actually write through."""

    dirs: list[str] = []
    if config.acquisition.backend == "torrent" and config.torrent.library_base_dir:
        dirs.append(config.torrent.library_base_dir)
    if config.subtitles.backend == "native" and config.subtitles.library_base_dir:
        dirs.append(config.subtitles.library_base_dir)
    return list(dict.fromkeys(dirs))


async def check_nas_mount(config: AppConfig) -> ProviderStatus:
    """The local library mount used for reliable NAS reads/writes (macOS TCC): the
    first problem found, or the last healthy directory."""

    dirs = _library_dirs(config)
    if not dirs:
        return ProviderStatus("nas-mount", False, None, "writing via SMB (no local mount set)")
    status = ProviderStatus("nas-mount", False, None)
    for base in dirs:
        status = await _check_library_dir(base)
        if status.ok is not True:
            break
    return status


async def _opensubtitles_token(http: httpx.AsyncClient, config: AppConfig, base: str) -> str:
    """A bearer token from the cache, logging in only when there isn't a live one."""

    global _os_token
    s = config.secrets
    username = s.opensubtitles_username
    if _os_token is not None:
        expires, cached_for, token = _os_token
        if cached_for == username and time.time() < expires:
            return token
    assert username is not None and s.opensubtitles_password is not None
    login = await http.post(
        f"{base}/login",
        json={"username": username, "password": s.opensubtitles_password.get_secret_value()},
        headers=_opensubtitles_headers(config),
    )
    login.raise_for_status()
    token = str(login.json().get("token"))
    _os_token = (time.time() + _OS_TOKEN_TTL_SECONDS, username, token)
    return token


def _opensubtitles_headers(config: AppConfig) -> dict[str, str]:
    key = config.secrets.opensubtitles_api_key
    assert key is not None
    return {
        "Api-Key": key.get_secret_value(),
        "User-Agent": config.subtitles.opensubtitles_user_agent,
        "Content-Type": "application/json",
    }


async def check_opensubtitles(config: AppConfig) -> ProviderStatus:
    """OpenSubtitles.com: reachability + today's remaining download quota. Out of
    quota counts as down: searches still work, but no subtitle can be fetched."""

    global _os_token
    if config.subtitles.backend != "native":
        return ProviderStatus("opensubtitles.com", False, None, "subtitles backend is 'bazarr'")
    s = config.secrets
    if s.opensubtitles_api_key is None:
        return ProviderStatus("opensubtitles.com", False, None, "no API key")
    headers = _opensubtitles_headers(config)
    base = "https://api.opensubtitles.com/api/v1"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            if not (s.opensubtitles_username and s.opensubtitles_password):
                (await http.get(f"{base}/infos/formats", headers=headers)).raise_for_status()
                return ProviderStatus("opensubtitles.com", True, True, "search only (no login)")
            token = await _opensubtitles_token(http, config, base)
            info = await http.get(
                f"{base}/infos/user", headers={**headers, "Authorization": f"Bearer {token}"}
            )
            if info.status_code == 401:  # revoked early: log in again next refresh
                _os_token = None
            info.raise_for_status()
            d = info.json().get("data", {})
            remaining = d.get("remaining_downloads", "?")
            detail = f"{remaining}/{d.get('allowed_downloads', '?')} downloads left today"
            exhausted = isinstance(remaining, int) and remaining <= 0
            return ProviderStatus("opensubtitles.com", True, not exhausted, detail)
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


def cached_statuses() -> list[ProviderStatus]:
    """The last probe's results however old, without probing: for the nav badge,
    which runs on every page and can't wait on the network."""

    return _cache[2] if _cache is not None else []


def clear_cache() -> None:
    """Drop cached results (tests / forced refresh)."""

    global _cache, _os_token
    _cache = None
    _os_token = None


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

        async def _bounded(name: str, coro: Any) -> ProviderStatus:
            try:
                return await asyncio.wait_for(coro, _PROBE_CAP_SECONDS)
            except TimeoutError:
                return ProviderStatus(name, True, False, "probe timed out")

        named: list[tuple[str, Any]] = [
            ("tmdb", check_tmdb(config)),
            ("omdb", check_omdb(config)),
            ("smb", check_smb(config)),
        ]
        if config.acquisition.backend == "torrent":
            named.append(("transmission", check_transmission(config)))
        else:
            named += [("radarr", check_radarr(config)), ("sonarr", check_sonarr(config))]
        # Whichever backend writes through the mount, a dead mount needs to show.
        if config.acquisition.backend == "torrent" or _library_dirs(config):
            named.append(("nas-mount", check_nas_mount(config)))
        if config.subtitles.backend == "native":
            named.append(("opensubtitles.com", check_opensubtitles(config)))
        else:
            named.append(("bazarr", check_bazarr(config)))

        statuses = list(await asyncio.gather(*(_bounded(n, c) for n, c in named)))
        with contextlib.suppress(TimeoutError):
            statuses += await asyncio.wait_for(check_subtitle_accounts(config), _PROBE_CAP_SECONDS)
        _cache = (now, key, statuses)
        return statuses
