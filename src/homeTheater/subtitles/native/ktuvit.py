"""ktuvit.me provider — Hebrew subtitles.

ktuvit has no public API; this mirrors the flow the Kodi/Bazarr ktuvit providers
use. Login is the fiddly part: ktuvit doesn't take the password directly — it
takes a value derived by scraping the site's rotating ``encryptionSalt`` and
running PBKDF2-HMAC-SHA1(salt, email) → AES-CBC(password, iv-from-email) →
SHA256 → base64. After login (cookie session) we ``SearchPage_search``, scrape
the film's subtitle ids off ``MovieInfo.aspx``, request a one-shot download
identifier, and fetch the file from ``DownloadFile.ashx``.

Hebrew-only. Supports both movies (subtitle ids off ``MovieInfo.aspx``) and
series per-episode (ids off ``GetModuleAjax.ashx`` for a given season/episode).
Every step is defensive — any failure yields an empty result, so a ktuvit outage
or a salt/format change never sinks a sweep (OpenSubtitles still covers Hebrew).
Needs a ktuvit.me account (``KTUVIT_*`` in .env).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ...db.models import TitleKind
from ...logging_setup import get_logger
from .base import SubtitleQuery, SubtitleResult

log = get_logger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Movies list ids on <a data-subtitle-id>; episodes on <input data-sub-id>.
_SUBTITLE_ID_RE = re.compile(r'data-subtitle-id="([^"]+)"')
_SUB_ID_RE = re.compile(r'data-sub-id="([^"]+)"')
_SALT_RE = re.compile(r"encryptionSalt\s*=\s*'([0-9A-Za-z]+)'")


class KtuvitSource:
    name = "ktuvit"

    def __init__(
        self,
        email: str,
        password: str,
        client: httpx.AsyncClient,
        *,
        base_url: str = "https://www.ktuvit.me",
        timeout: float = 20.0,
    ) -> None:
        self._email = email
        self._password = password
        self._client = client
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._logged_in = False

    def supports(self, lang: str) -> bool:
        return lang == "he"

    def _svc_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": _BROWSER_UA,
            "Referer": self._base + "/",
        }

    async def _encrypted_password(self) -> str:
        """ktuvit's bespoke password derivation (salt scraped from the homepage)."""

        home = await self._client.get(
            self._base + "/", headers={"User-Agent": _BROWSER_UA}, timeout=self._timeout
        )
        home.raise_for_status()
        m = _SALT_RE.search(home.text)
        if not m:
            raise ValueError("could not find encryptionSalt on the ktuvit homepage")
        salt = m.group(1).encode()
        key = hashlib.pbkdf2_hmac("sha1", salt, self._email.encode(), 3000, 16)
        encryptor = Cipher(algorithms.AES(key), modes.CBC(_iv_from_email(self._email))).encryptor()
        data = encryptor.update(_pkcs7(self._password.encode())) + encryptor.finalize()
        return base64.b64encode(hashlib.sha256(data).digest()).decode()

    async def _login(self) -> bool:
        if self._logged_in:
            return True
        password = await self._encrypted_password()
        resp = await self._client.post(
            f"{self._base}/Services/MembershipService.svc/Login",
            json={"request": {"Email": self._email, "Password": password}},
            headers=self._svc_headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        # Session lives in the client cookie jar (Set-Cookie: Login=...).
        self._logged_in = "Login" in resp.cookies or bool(_unwrap(resp.json()))
        return self._logged_in

    async def search(self, query: SubtitleQuery) -> list[SubtitleResult]:
        is_series = query.kind is TitleKind.series
        if is_series and (query.season is None or query.episode is None):
            return []  # can't target an episode without season/episode
        try:
            if not await self._login():
                return []
            content_id = await self._find_content(
                query.title, query.year, search_type="1" if is_series else "0"
            )
            if content_id is None:
                return []
            if is_series:
                sub_ids = await self._episode_subtitle_ids(content_id, query.season, query.episode)
            else:
                sub_ids = await self._movie_subtitle_ids(content_id)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("ktuvit.search_failed", title=query.title, detail=str(exc))
            return []
        return [
            SubtitleResult(
                source=self.name,
                lang="he",
                name=f"ktuvit:{content_id}:{sid}",
                score=float(len(sub_ids) - i),  # ktuvit lists best first
                ref={"film_id": content_id, "subtitle_id": sid},
            )
            for i, sid in enumerate(sub_ids)
        ]

    async def _find_content(self, title: str, year: int | None, *, search_type: str) -> str | None:
        req: dict[str, Any] = {
            "FilmName": title,
            "Actors": [],
            "Studios": None,
            "Directors": [],
            "Genres": [],
            "Countries": [],
            "Languages": [],
            "Year": str(year) if year else "",
            "Rating": [],
            "Page": 1,
            "SearchType": search_type,  # 0 = movies, 1 = series
            # False, not True: WithSubsOnly filters out series here (and we check
            # for real subtitle ids in the next step regardless).
            "WithSubsOnly": False,
        }
        resp = await self._client.post(
            f"{self._base}/Services/ContentProvider.svc/SearchPage_search",
            json={"request": req},
            headers=self._svc_headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        films = _unwrap(resp.json()).get("Films") or []
        return str(films[0]["ID"]) if films else None

    async def _movie_subtitle_ids(self, film_id: str) -> list[str]:
        resp = await self._client.get(
            f"{self._base}/MovieInfo.aspx",
            params={"ID": film_id},
            headers={"User-Agent": _BROWSER_UA},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return _dedupe(_SUBTITLE_ID_RE.findall(resp.text))

    async def _episode_subtitle_ids(
        self, series_id: str, season: int | None, episode: int | None
    ) -> list[str]:
        resp = await self._client.get(
            f"{self._base}/Services/GetModuleAjax.ashx",
            params={
                "moduleName": "SubtitlesList",
                "SeriesID": series_id,
                "Season": season,
                "Episode": episode,
            },
            headers={"User-Agent": _BROWSER_UA},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return _dedupe(_SUB_ID_RE.findall(resp.text))

    async def download(self, result: SubtitleResult) -> bytes:
        await self._login()
        req = {
            "FilmID": result.ref["film_id"],
            "SubtitleID": result.ref["subtitle_id"],
            "FontSize": 0,
            "FontColor": "",
            "PredefinedLayout": -1,
        }
        resp = await self._client.post(
            f"{self._base}/Services/ContentProvider.svc/RequestSubtitleDownload",
            json={"request": req},
            headers=self._svc_headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        identifier = _unwrap(resp.json()).get("DownloadIdentifier")
        if not identifier:
            raise ValueError("ktuvit returned no DownloadIdentifier")
        got = await self._client.get(
            f"{self._base}/Services/DownloadFile.ashx",
            params={"DownloadIdentifier": identifier},
            headers={"User-Agent": _BROWSER_UA},
            timeout=self._timeout,
        )
        got.raise_for_status()
        return _extract_srt(got.content)


def _dedupe(ids: list[str]) -> list[str]:
    """De-dup subtitle ids preserving ktuvit's order (best first)."""

    seen: dict[str, None] = {}
    for sid in ids:
        seen.setdefault(sid, None)
    return list(seen)


def _iv_from_email(data: str) -> bytes:
    """ktuvit's AES IV: each char of the email read as a hex digit into a byte,
    truncated/zero-padded to 16 bytes."""

    iv: list[int] = []
    for i in range(0, len(data), 2):
        hx = ""
        for c in data[i : i + 2]:
            try:
                int(c, 16)
                hx += c
            except ValueError:
                break
        iv.append(int(hx or "0", 16))
    iv = iv[:16] + [0] * (16 - len(iv))
    return bytes(iv[:16])


def _pkcs7(m: bytes) -> bytes:
    pad = 16 - len(m) % 16
    return m + bytes([pad]) * pad


def _unwrap(body: Any) -> dict[str, Any]:
    """ktuvit wraps service payloads as ``{"d": "<json string>"}``."""

    inner = body.get("d", body) if isinstance(body, dict) else body
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            return {}
    return inner if isinstance(inner, dict) else {}


def _extract_srt(data: bytes) -> bytes:
    """ktuvit hands back either a raw subtitle or a zip; return the .srt bytes."""

    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".srt", ".sub", ".ass"))]
            if names:
                return zf.read(names[0])
    return data
