"""Payload safety: bounded fetch, unpacking, decoding, validation, ranking."""

from __future__ import annotations

import gzip
import io
import tracemalloc
import zipfile
import zlib

import httpx
import pytest
import respx

from homeTheater.subtitles.native.base import InvalidSubtitleError, SubtitleResult
from homeTheater.subtitles.native.payload import (
    MAX_COMPRESSED_BYTES,
    MAX_DECOMPRESSED_BYTES,
    decode_subtitle,
    fetch_capped,
    unpack_subtitle,
    validate_subtitle,
)
from homeTheater.subtitles.native.ranking import rank_results, release_overlap

SRT = "1\n00:00:01,000 --> 00:00:02,000\n{}\n\n".format
ASS = (
    "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
    "Format: Layer, Start, End, Text\nDialogue: 0,0:00:01.00,0:00:02.00,hi\n"
)
SHALOM = "שלום עולם"


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- validate_subtitle ------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"   \n",
        b"<!DOCTYPE html><html><body>Download limit reached</body></html>",
        b"\n <html><body>\n00:00:01,000 --> 00:00:02,000\n</body></html>",
        b"Rar!\x1a\x07\x00" + bytes(range(256)) * 20,
        b"just some text with no timings at all",
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n",
    ],
)
def test_validate_rejects_non_subtitles(payload: bytes) -> None:
    with pytest.raises(InvalidSubtitleError):
        validate_subtitle(payload, "en")


def test_validate_keeps_the_true_extension_for_ass_and_ssa() -> None:
    assert validate_subtitle(SRT("hi").encode(), "en").ext == ".srt"
    assert validate_subtitle(ASS.encode(), "en").ext == ".ass"
    assert validate_subtitle(ASS.replace("v4.00+", "v4.00").encode(), "en").ext == ".ssa"


# --- encodings --------------------------------------------------------------


def test_cp1255_hebrew_is_rewritten_as_utf8() -> None:
    out = validate_subtitle(SRT(SHALOM).encode("cp1255"), "he")
    assert SHALOM in out.data.decode("utf-8")


def test_utf8_hebrew_is_not_mistaken_for_cp1255() -> None:
    assert SHALOM in decode_subtitle(SRT(SHALOM).encode("utf-8"), "he")


def test_utf8_bom_is_dropped() -> None:
    out = validate_subtitle(b"\xef\xbb\xbf" + SRT(SHALOM).encode("utf-8"), "he")
    assert not out.data.startswith(b"\xef\xbb\xbf") and out.data.startswith(b"1\n")


@pytest.mark.parametrize(
    ("raw_encoding", "bom"),
    [("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"), ("utf-32-le", b"\xff\xfe\x00\x00")],
)
def test_utf16_and_32_with_bom(raw_encoding: str, bom: bytes) -> None:
    out = validate_subtitle(bom + SRT(SHALOM).encode(raw_encoding), "he")
    assert SHALOM in out.data.decode("utf-8")


@pytest.mark.parametrize("raw_encoding", ["utf-16-le", "utf-16-be"])
def test_utf16_without_bom(raw_encoding: str) -> None:
    out = validate_subtitle(SRT(SHALOM).encode(raw_encoding), "he")
    assert SHALOM in out.data.decode("utf-8")


def test_stray_nuls_are_removed() -> None:
    out = validate_subtitle(SRT("hi").encode() + b"\x00\x00", "en")
    assert b"\x00" not in out.data


def test_undecodable_hebrew_is_rejected_not_written_as_mojibake() -> None:
    with pytest.raises(InvalidSubtitleError):
        decode_subtitle(b"\xfb\xfc\xff" * 100, "he")


# --- gzip -------------------------------------------------------------------


def test_truncated_gzip_raises() -> None:
    whole = gzip.compress(SRT("hi").encode() * 50)
    with pytest.raises(InvalidSubtitleError, match="truncated"):
        unpack_subtitle(whole[:-12])


def test_gzip_bomb_is_stopped_at_the_limit() -> None:
    bomb = gzip.compress(b"\x00" * (MAX_DECOMPRESSED_BYTES + 1024))
    assert len(bomb) < MAX_COMPRESSED_BYTES
    with pytest.raises(InvalidSubtitleError, match="limit"):
        unpack_subtitle(bomb)


def test_gzip_bomb_is_never_expanded_into_memory() -> None:
    packer = zlib.compressobj(wbits=31)
    block = b"\x00" * (1024 * 1024)
    bomb = b"".join(packer.compress(block) for _ in range(64)) + packer.flush()
    tracemalloc.start()
    try:
        with pytest.raises(InvalidSubtitleError):
            unpack_subtitle(bomb)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < MAX_DECOMPRESSED_BYTES * 3


def test_plain_payload_passes_through() -> None:
    assert unpack_subtitle(b"1\nplain") == b"1\nplain"


# --- zip --------------------------------------------------------------------


def test_zip_skips_macosx_metadata() -> None:
    data = _zip(
        {
            "__MACOSX/._movie.srt": b"\x00\x05junk" * 500,
            "._movie.srt": b"\x00\x05junk" * 500,
            "movie.srt": SRT("real").encode(),
        }
    )
    assert b"real" in unpack_subtitle(data)


def test_zip_picks_the_episode_not_the_first_name() -> None:
    data = _zip(
        {
            "Show.S02E01.srt": SRT("one").encode() * 100,
            "Show.S02E03.srt": SRT("three").encode(),
            "Show.S02E05.srt": SRT("five").encode() * 100,
        }
    )
    assert b"three" in unpack_subtitle(data, season=2, episode=3)


def test_zip_without_the_episode_is_refused() -> None:
    data = _zip({"Show.S02E01.srt": b"a", "Show.S02E02.srt": b"b"})
    with pytest.raises(InvalidSubtitleError):
        unpack_subtitle(data, season=2, episode=9)


def test_single_member_labelled_as_another_episode_is_refused() -> None:
    with pytest.raises(InvalidSubtitleError):
        unpack_subtitle(_zip({"Show.S02E01.srt": b"a"}), season=2, episode=9)


def test_single_unlabelled_member_is_accepted_for_an_episode() -> None:
    assert unpack_subtitle(_zip({"subtitle.srt": b"x"}), season=2, episode=9) == b"x"


def test_zip_movie_takes_the_largest_srt() -> None:
    data = _zip(
        {
            "cover.srt": SRT("small").encode(),
            "movie.srt": SRT("big").encode() * 50,
            "movie.ass": ASS.encode() * 500,
            "readme.txt": b"x" * 9000,
        }
    )
    assert b"big" in unpack_subtitle(data)


def test_zip_member_over_the_size_limit_is_skipped() -> None:
    data = _zip({"huge.srt": b"\x00" * (MAX_DECOMPRESSED_BYTES + 1), "ok.srt": b"fine"})
    assert unpack_subtitle(data) == b"fine"


def test_zip_with_understated_member_size_is_still_bounded() -> None:
    data = bytearray(_zip({"movie.srt": b"A" * (MAX_DECOMPRESSED_BYTES + 1000)}))
    real = (MAX_DECOMPRESSED_BYTES + 1000).to_bytes(4, "little")
    fake = (100).to_bytes(4, "little")
    assert data.count(real) == 2  # local header + central directory
    with pytest.raises(InvalidSubtitleError):
        unpack_subtitle(bytes(data.replace(real, fake)))


@pytest.mark.parametrize("data", [b"PK\x03\x04broken", _zip({"notes.txt": b"x"})])
def test_zip_without_a_subtitle_is_invalid(data: bytes) -> None:
    with pytest.raises(InvalidSubtitleError):
        unpack_subtitle(data)


# --- fetch_capped -----------------------------------------------------------


@respx.mock
async def test_fetch_stops_at_the_compressed_cap() -> None:
    respx.get("https://dl.test/big").mock(return_value=httpx.Response(200, content=b"x" * 5000))
    async with httpx.AsyncClient() as http:
        with pytest.raises(InvalidSubtitleError):
            await fetch_capped(http, "https://dl.test/big", source="t", timeout=5, max_bytes=1000)


@respx.mock
async def test_fetch_stops_a_body_with_no_declared_length() -> None:
    respx.get("https://dl.test/big").mock(
        return_value=httpx.Response(200, stream=httpx.ByteStream(b"x" * 5000))
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(InvalidSubtitleError):
            await fetch_capped(http, "https://dl.test/big", source="t", timeout=5, max_bytes=1000)


@respx.mock
async def test_fetch_refuses_a_declared_oversize_body() -> None:
    respx.get("https://dl.test/big").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "99999999"}, content=b"x")
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(InvalidSubtitleError):
            await fetch_capped(http, "https://dl.test/big", source="t", timeout=5)


@respx.mock
async def test_fetch_does_not_inflate_content_encoding_past_the_cap() -> None:
    bomb = gzip.compress(b"\x00" * (MAX_DECOMPRESSED_BYTES * 4))
    respx.get("https://dl.test/enc").mock(
        return_value=httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=bomb)
    )
    async with httpx.AsyncClient() as http:
        raw = await fetch_capped(http, "https://dl.test/enc", source="t", timeout=5)
    assert raw == bomb  # still compressed: unpack_subtitle applies the limit
    with pytest.raises(InvalidSubtitleError):
        unpack_subtitle(raw)


@respx.mock
async def test_fetch_follows_redirects() -> None:
    respx.get("https://dl.test/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://dl.test/b"})
    )
    respx.get("https://dl.test/b").mock(return_value=httpx.Response(200, content=b"ok"))
    async with httpx.AsyncClient() as http:
        assert await fetch_capped(http, "https://dl.test/a", source="t", timeout=5) == b"ok"


# --- ranking ----------------------------------------------------------------


def _result(name: str, score: float = 0.0, **kw: bool) -> SubtitleResult:
    return SubtitleResult(source="s", lang="en", name=name, score=score, **kw)


MEDIA = "Movie.2020.1080p.BluRay.x264-SPARKS"


def test_release_overlap_counts_shared_tokens() -> None:
    assert release_overlap(MEDIA, "Movie.2020.1080p.BluRay.x264-SPARKS") == 10
    assert release_overlap(MEDIA, "Movie.2020.720p.WEB-DL.H264-NTb") == 1  # codec only
    assert release_overlap(MEDIA, "") == 0


def test_matching_release_beats_popularity() -> None:
    popular = _result("Movie.2020.720p.WEB-DL-NTb", score=9000)
    matched = _result("Movie.2020.1080p.BluRay.x264-SPARKS", score=1)
    assert rank_results([popular, matched], MEDIA, hearing_impaired=False)[0] is matched


def test_hash_match_beats_release_match() -> None:
    hashed = _result("whatever", hash_match=True)
    matched = _result("Movie.2020.1080p.BluRay.x264-SPARKS", score=9000)
    assert rank_results([matched, hashed], MEDIA, hearing_impaired=False)[0] is hashed


@pytest.mark.parametrize("wanted", [True, False])
def test_hearing_impaired_preference_decides_a_tie(wanted: bool) -> None:
    plain = _result("Movie.2020.1080p", hearing_impaired=False)
    captions = _result("Movie.2020.1080p", hearing_impaired=True)
    top = rank_results([plain, captions], MEDIA, hearing_impaired=wanted)[0]
    assert top.hearing_impaired is wanted
