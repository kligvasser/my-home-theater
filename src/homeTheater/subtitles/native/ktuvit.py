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
A failure raises (see :class:`SubtitleSourceError`) rather than looking like "no
subtitles": the sweep falls through to OpenSubtitles and disables ktuvit if it keeps
failing. A title is matched by imdb id or by name + year; anything less returns
nothing. Needs a ktuvit.me account (``KTUVIT_*`` in .env).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ...db.models import TitleKind
from ...logging_setup import get_logger
from .base import (
    SourceAuthError,
    SubtitleQuery,
    SubtitleResult,
    SubtitleSourceError,
    raise_for_http,
)
from .payload import fetch_capped, unpack_subtitle

log = get_logger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Movies list ids on <a data-subtitle-id>; episodes on <input data-sub-id>.
_SUBTITLE_ID_RE = re.compile(r'data-subtitle-id="([^"]+)"')
_SUB_ID_RE = re.compile(r'data-sub-id="([^"]+)"')
_SALT_RE = re.compile(r"encryptionSalt\s*=\s*'([0-9A-Za-z]+)'")
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_IMDB_RE = re.compile(r"tt\d+", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# Search-result fields, as far as the site exposes them; see _match_film.
_NAME_FIELDS = ("EngName", "HebName", "Name", "FilmName")
_YEAR_FIELDS = ("Year", "ReleaseDate", "ReleaseYear")
_IMDB_FIELDS = ("IMDB_Link", "ImdbID", "IMDB_ID", "ImdbId", "IMDBID")
_YEAR_TOLERANCE = 1  # sites disagree by a year around a January release


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
        raise_for_http(home, self.name)
        m = _SALT_RE.search(home.text)
        if not m:
            raise SubtitleSourceError("ktuvit: no encryptionSalt on the homepage")
        salt = m.group(1).encode()
        key = hashlib.pbkdf2_hmac("sha1", salt, self._email.encode(), 3000, 16)
        encryptor = Cipher(algorithms.AES(key), modes.CBC(_iv_from_email(self._email))).encryptor()
        data = encryptor.update(_pkcs7(self._password.encode())) + encryptor.finalize()
        return base64.b64encode(hashlib.sha256(data).digest()).decode()

    async def _login(self) -> None:
        if self._logged_in:
            return
        password = await self._encrypted_password()
        resp = await self._client.post(
            f"{self._base}/Services/MembershipService.svc/Login",
            json={"request": {"Email": self._email, "Password": password}},
            headers=self._svc_headers(),
            timeout=self._timeout,
        )
        raise_for_http(resp, self.name)
        # A successful login sets the ``Login`` session cookie. Don't fall back to
        # "any non-empty body" — a failed login returns a non-empty error payload.
        self._logged_in = "Login" in resp.cookies
        if not self._logged_in:
            raise SourceAuthError("ktuvit: login set no session cookie (check credentials)")

    async def search(self, query: SubtitleQuery) -> list[SubtitleResult]:
        if not query.searchable:
            return []  # can't target an episode without season/episode
        is_series = query.kind is TitleKind.series
        await self._login()
        try:
            content_id = await self._find_content(query, search_type="1" if is_series else "0")
            if content_id is None:
                return []
            if is_series:
                rows = await self._episode_subtitles(content_id, query.season, query.episode)
            else:
                rows = await self._movie_subtitles(content_id)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SubtitleSourceError("ktuvit: unexpected response format") from exc
        return [
            SubtitleResult(
                source=self.name,
                lang="he",
                name=release or f"ktuvit:{content_id}:{sid}",
                score=float(len(rows) - i),  # ktuvit lists best first
                ref={"film_id": content_id, "subtitle_id": sid},
                season=query.season,
                episode=query.episode,
            )
            for i, (sid, release) in enumerate(rows)
        ]

    async def _find_content(self, query: SubtitleQuery, *, search_type: str) -> str | None:
        req: dict[str, Any] = {
            "FilmName": query.title,
            "Actors": [],
            "Studios": None,
            "Directors": [],
            "Genres": [],
            "Countries": [],
            "Languages": [],
            "Year": str(query.year) if query.year else "",
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
        raise_for_http(resp, self.name)
        films = _unwrap(resp.json()).get("Films") or []
        return _match_film(films, query)

    async def _movie_subtitles(self, film_id: str) -> list[tuple[str, str]]:
        resp = await self._client.get(
            f"{self._base}/MovieInfo.aspx",
            params={"ID": film_id},
            headers={"User-Agent": _BROWSER_UA},
            timeout=self._timeout,
        )
        raise_for_http(resp, self.name)
        return _subtitle_rows(resp.text, _SUBTITLE_ID_RE)

    async def _episode_subtitles(
        self, series_id: str, season: int | None, episode: int | None
    ) -> list[tuple[str, str]]:
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
        raise_for_http(resp, self.name)
        return _subtitle_rows(resp.text, _SUB_ID_RE)

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
        raise_for_http(resp, self.name)
        identifier = _unwrap(resp.json()).get("DownloadIdentifier")
        if not identifier:
            raise SubtitleSourceError("ktuvit: no DownloadIdentifier in the response")
        data = await fetch_capped(
            self._client,
            f"{self._base}/Services/DownloadFile.ashx",
            source=self.name,
            timeout=self._timeout,
            params={"DownloadIdentifier": identifier},
            headers={"User-Agent": _BROWSER_UA},
        )
        return unpack_subtitle(data, result.season, result.episode)


def _normalise_name(name: str) -> str:
    """Compare names without case, accents, niqqud or punctuation."""

    decomposed = unicodedata.normalize("NFKD", name.casefold().replace("&", " and "))
    kept = [
        c if c.isalnum() else " " for c in decomposed if not unicodedata.category(c).startswith("M")
    ]
    return " ".join("".join(kept).split())


def _first_match(film: dict[str, Any], fields: tuple[str, ...], pattern: re.Pattern[str]) -> str:
    for field in fields:
        found = pattern.search(str(film.get(field) or ""))
        if found:
            return found.group(0).lower()
    return ""


def _match_film(films: list[dict[str, Any]], query: SubtitleQuery) -> str | None:
    """The search hit that is really this title, or ``None``.

    An imdb id on both sides settles it either way. Otherwise a normalised English
    or Hebrew name must match and the year must be within tolerance; several such
    hits, or a missing year, mean we would be guessing.

    ktuvit's field names aren't documented. If a response has hits but none of the
    expected fields, that is a format change and raises, instead of quietly
    finding nothing for every title.
    """

    if films and not any(
        film.get(f) for film in films for f in (*_NAME_FIELDS, *_YEAR_FIELDS, *_IMDB_FIELDS)
    ):
        raise SubtitleSourceError("ktuvit: search results have no recognisable fields")

    wanted_imdb = (query.imdb_id or "").lower()
    wanted_name = _normalise_name(query.title)
    close: list[str] = []
    for film in films:
        imdb = _first_match(film, _IMDB_FIELDS, _IMDB_RE)
        if wanted_imdb and imdb:
            if imdb == wanted_imdb:
                return str(film["ID"])
            continue
        names = {_normalise_name(str(film.get(f) or "")) for f in _NAME_FIELDS}
        year = _first_match(film, _YEAR_FIELDS, _YEAR_RE)
        near = bool(query.year and year) and abs(int(year) - (query.year or 0)) <= _YEAR_TOLERANCE
        if wanted_name in names and near:
            close.append(str(film["ID"]))
    return close[0] if len(set(close)) == 1 else None


def _subtitle_rows(html: str, id_re: re.Pattern[str]) -> list[tuple[str, str]]:
    """``(subtitle id, release name)`` in page order, best first, de-duplicated.

    The release name is the first line of the row's first cell; it is empty when
    the markup doesn't have that shape, which only costs the release-match bonus.
    """

    releases: dict[str, str] = {}
    for row in _ROW_RE.findall(html):
        sid = id_re.search(row)
        cell = _CELL_RE.search(row)
        if sid and cell:
            lines = _TAG_RE.sub("\n", cell.group(1)).splitlines()
            releases.setdefault(sid.group(1), next((x.strip() for x in lines if x.strip()), ""))
    return [(sid, releases.get(sid, "")) for sid in _dedupe(id_re.findall(html))]


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
