"""Subtitle payload handling: bounded fetch, unpack, decode, validate.

Everything a provider sends is untrusted. Downloads are capped while streaming,
gzip and zip are expanded with hard limits, and :func:`validate_subtitle` is the
single gate between a payload and the disk: it rejects anything that isn't a real
subtitle and returns UTF-8 bytes plus the extension that matches the format.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import Any

import httpx
from guessit import guessit

from .base import InvalidSubtitleError, raise_for_http

MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 5 * 1024 * 1024
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa")

_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK"
_BOMS = (  # UTF-32 first: its little-endian BOM starts with the UTF-16 one
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
_LEGACY_CODEPAGES = {"he": "cp1255", "ar": "cp1256", "ru": "cp1251"}
_DEFAULT_CODEPAGE = "cp1252"
_SRT_TIMING_RE = re.compile(
    r"^\s*\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}", re.MULTILINE
)
_ASS_DIALOGUE_RE = re.compile(r"^\s*Dialogue\s*:", re.MULTILINE | re.IGNORECASE)
_HTML_PREFIXES = ("<!doctype", "<html", "<head", "<body", "<?xml")


@dataclass(frozen=True, slots=True)
class ValidatedSubtitle:
    data: bytes  # UTF-8, no BOM
    ext: str  # one of SUBTITLE_EXTENSIONS


async def fetch_capped(
    client: httpx.AsyncClient,
    url: str,
    *,
    source: str,
    timeout: float,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_COMPRESSED_BYTES,
) -> bytes:
    """GET ``url`` keeping at most ``max_bytes``, following redirects.

    Reads the raw stream on purpose: httpx's decoded stream would expand a
    ``Content-Encoding: gzip`` body before any cap applied. Only gzip is accepted,
    and :func:`unpack_subtitle` expands that with a limit.
    """

    request_headers = {**(headers or {}), "Accept-Encoding": "gzip"}
    async with client.stream(
        "GET",
        url,
        params=params,
        headers=request_headers,
        timeout=timeout,
        follow_redirects=True,
    ) as resp:
        raise_for_http(resp, source)
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise InvalidSubtitleError(f"{source}: payload of {declared} bytes is too large")
        body = bytearray()
        async for chunk in resp.aiter_raw():
            body += chunk
            if len(body) > max_bytes:
                raise InvalidSubtitleError(f"{source}: payload exceeds {max_bytes} bytes")
    return bytes(body)


def _gunzip(data: bytes) -> bytes:
    inflater = zlib.decompressobj(wbits=31)  # 31: gzip container
    try:
        out = inflater.decompress(data, MAX_DECOMPRESSED_BYTES + 1)
    except zlib.error as exc:
        raise InvalidSubtitleError("corrupt gzip payload") from exc
    if len(out) > MAX_DECOMPRESSED_BYTES:
        raise InvalidSubtitleError("gzip payload expands past the size limit")
    if not inflater.eof:
        raise InvalidSubtitleError("truncated gzip payload")
    return out


def _labelled_episode(name: str) -> tuple[Any, Any]:
    """(season, episode) a zip member's name claims; a value may be a list."""

    try:
        info = guessit(os.path.basename(name))
    except Exception:  # guessit raises its own errors on odd names; treat as unlabelled
        return None, None
    return info.get("season"), info.get("episode")


def _matches(value: Any, wanted: int) -> bool:
    return wanted in value if isinstance(value, list) else value == wanted


def _pick_member(
    zf: zipfile.ZipFile, season: int | None, episode: int | None
) -> zipfile.ZipInfo | None:
    members = [
        m
        for m in zf.infolist()
        if not m.is_dir()
        and not m.flag_bits & 0x1  # encrypted
        and m.file_size <= MAX_DECOMPRESSED_BYTES
        and "__MACOSX" not in m.filename.split("/")
        and not os.path.basename(m.filename).startswith("._")
        and m.filename.lower().endswith(SUBTITLE_EXTENSIONS)
    ]
    if not members:
        return None
    if episode is not None:
        exact = []
        for m in members:
            got_season, got_episode = _labelled_episode(m.filename)
            season_ok = got_season is None or season is None or _matches(got_season, season)
            if got_episode is not None and _matches(got_episode, episode) and season_ok:
                exact.append(m)
        if exact:
            members = exact
        elif len(members) > 1 or _labelled_episode(members[0].filename)[1] is not None:
            return None  # a pack without this episode; any member would be the wrong one
    return max(members, key=lambda m: (m.filename.lower().endswith(".srt"), m.file_size))


def _unzip(data: bytes, season: int | None, episode: int | None) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = _pick_member(zf, season, episode)
            if member is None:
                raise InvalidSubtitleError("zip holds no subtitle for this file")
            # zipfile stops at the header's file_size, checked above, and a member
            # that lied about it then fails its CRC.
            return zf.read(member)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as exc:
        raise InvalidSubtitleError("corrupt zip payload") from exc


def unpack_subtitle(data: bytes, season: int | None = None, episode: int | None = None) -> bytes:
    """Expand a gzip and/or zip payload; plain payloads pass through unchanged."""

    if data[:2] == _GZIP_MAGIC:
        data = _gunzip(data)
    if data[:2] == _ZIP_MAGIC:
        return _unzip(data, season, episode)
    if len(data) > MAX_DECOMPRESSED_BYTES:
        raise InvalidSubtitleError("payload exceeds the size limit")
    return data


def _sniff_bomless_utf16(data: bytes) -> str | None:
    """UTF-16 without a BOM: timecodes are ASCII, so one side is full of NULs."""

    sample = data[:4096]
    pairs = len(sample) // 2
    if pairs < 8:
        return None
    even = sample[0::2].count(0)
    odd = sample[1::2].count(0)
    if odd >= pairs * 0.2 and even == 0:
        return "utf-16-le"
    if even >= pairs * 0.2 and odd == 0:
        return "utf-16-be"
    return None


def _decode_wide(data: bytes, encoding: str) -> str:
    width = 4 if encoding.startswith("utf-32") else 2
    try:
        return data[: len(data) - len(data) % width].decode(encoding)
    except UnicodeDecodeError as exc:
        raise InvalidSubtitleError(f"not valid {encoding}") from exc


def decode_subtitle(data: bytes, lang: str) -> str:
    """BOM, then UTF-16 without BOM, then strict UTF-8, then the language's codepage."""

    for bom, encoding in _BOMS:
        if data.startswith(bom):
            body = data[len(bom) :]
            if encoding == "utf-8":
                try:
                    return body.decode("utf-8").replace("\x00", "")
                except UnicodeDecodeError as exc:
                    raise InvalidSubtitleError("not valid utf-8") from exc
            return _decode_wide(body, encoding).replace("\x00", "")
    wide = _sniff_bomless_utf16(data)
    if wide is not None:
        return _decode_wide(data, wide).replace("\x00", "")

    if data.count(0) > max(8, len(data) // 1000):
        raise InvalidSubtitleError("binary payload")
    data = data.replace(b"\x00", b"")  # stray padding, seen at the end of old files
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    text = data.decode(_LEGACY_CODEPAGES.get(lang, _DEFAULT_CODEPAGE), errors="replace")
    if text.count("�") > len(text) // 200:
        raise InvalidSubtitleError(f"undecodable text for language {lang!r}")
    return text


def validate_subtitle(data: bytes, lang: str) -> ValidatedSubtitle:
    """Return ``data`` as UTF-8 plus its true extension, or raise InvalidSubtitleError."""

    if not data.strip():
        raise InvalidSubtitleError("empty payload")
    text = decode_subtitle(data, lang)
    head = text[:2048].lstrip("﻿ \t\r\n")
    lowered = head.lower()
    if lowered.startswith(_HTML_PREFIXES):
        raise InvalidSubtitleError("HTML page, not a subtitle")
    if lowered.startswith("webvtt"):
        raise InvalidSubtitleError("WebVTT is not supported")

    controls = sum(1 for c in text if ord(c) < 32 and c not in "\t\n\r\f")
    if controls > len(text) // 100:
        raise InvalidSubtitleError("binary payload")

    ext: str | None = None
    if _SRT_TIMING_RE.search(text):
        ext = ".srt"
    elif "[script info]" in lowered and _ASS_DIALOGUE_RE.search(text):
        ext = ".ass" if "v4.00+" in lowered else ".ssa"
    if ext is None:
        raise InvalidSubtitleError("no subtitle timings found")
    return ValidatedSubtitle(data=text.encode("utf-8"), ext=ext)
