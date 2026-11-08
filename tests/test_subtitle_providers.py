"""Provider clients: error typing, quotas, filtering, title matching. HTTP is mocked."""

from __future__ import annotations

import io
import json
import time
import xmlrpc.client
import zipfile
from typing import Any

import httpx
import pytest
import respx

from homeTheater.db.models import TitleKind
from homeTheater.subtitles.native.base import (
    SourceAuthError,
    SourceQuotaError,
    SubtitleQuery,
    SubtitleResult,
    SubtitleSourceError,
)
from homeTheater.subtitles.native.ktuvit import KtuvitSource
from homeTheater.subtitles.native.opensubtitles import OpenSubtitlesSource
from homeTheater.subtitles.native.opensubtitles_org import OpenSubtitlesOrgSource

OS_BASE = "https://api.opensubtitles.com/api/v1"
ORG = "https://api.opensubtitles.org/xml-rpc"
KTUVIT = "https://www.ktuvit.me"


def _movie(**kw: Any) -> SubtitleQuery:
    base: dict[str, Any] = {
        "lang": "en",
        "kind": TitleKind.movie,
        "title": "The Matrix",
        "year": 1999,
        "imdb_id": "tt0133093",
        "release_name": "The.Matrix.1999",
    }
    return SubtitleQuery(**{**base, **kw})


def _episode(**kw: Any) -> SubtitleQuery:
    base: dict[str, Any] = {
        "kind": TitleKind.series,
        "title": "Show",
        "year": 2020,
        "imdb_id": "tt1234567",
        "season": 2,
        "episode": 3,
    }
    return _movie(**{**base, **kw})


def _rpc(value: object) -> httpx.Response:
    return httpx.Response(200, text=xmlrpc.client.dumps((value,), methodresponse=True))


def _org(http: httpx.AsyncClient) -> OpenSubtitlesOrgSource:
    return OpenSubtitlesOrgSource("u", "p", http, min_interval=0)


LOGIN_OK = {"token": "T", "status": "200 OK"}


# --- opensubtitles.org ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("401 Unauthorized", SourceAuthError),
        ("414 Unknown User Agent", SourceAuthError),
        ("407 Download limit reached", SourceQuotaError),
        ("429 Too many requests", SourceQuotaError),
        ("503 Service Unavailable", SubtitleSourceError),
        ("", SubtitleSourceError),
    ],
)
@respx.mock
async def test_org_search_status_is_not_mistaken_for_no_results(
    status: str, expected: type[Exception]
) -> None:
    respx.post(ORG).mock(side_effect=[_rpc(LOGIN_OK), _rpc({"status": status, "data": False})])
    async with httpx.AsyncClient() as http:
        with pytest.raises(expected):
            await _org(http).search(_movie())


@respx.mock
async def test_org_rejected_login_is_an_auth_error() -> None:
    respx.post(ORG).mock(return_value=_rpc({"status": "401 Unauthorized"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceAuthError):
            await _org(http).search(_movie())


@respx.mock
async def test_org_html_body_is_a_provider_error() -> None:
    respx.post(ORG).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(SubtitleSourceError):
            await _org(http).search(_movie())


@respx.mock
async def test_org_splits_hash_and_imdb_criteria() -> None:
    route = respx.post(ORG).mock(
        side_effect=[_rpc(LOGIN_OK), _rpc({"status": "200 OK", "data": False})]
    )
    async with httpx.AsyncClient() as http:
        await _org(http).search(_episode(moviehash="abc", filesize=123))

    (_, criteria), _ = xmlrpc.client.loads(route.calls.last.request.content.decode())
    assert len(criteria) == 2
    assert criteria[0]["moviehash"] == "abc" and "imdbid" not in criteria[0]
    assert criteria[1]["imdbid"] == "1234567" and "moviehash" not in criteria[1]
    assert criteria[1]["season"] == "2" and criteria[1]["episode"] == "3"


@respx.mock
async def test_org_drops_flagged_results_and_duplicate_links() -> None:
    row = {"SubDownloadLink": "https://dl.org/ok.gz", "SubFileName": "ok", "SubDownloadsCnt": "1"}
    data = [
        row,
        {**row, "MatchedBy": "moviehash", "SubDownloadsCnt": "5"},  # same file via the hash
        {**row, "SubDownloadLink": "https://dl.org/bad.gz", "SubBad": "1"},
        {**row, "SubDownloadLink": "https://dl.org/ai.gz", "SubAutoTranslation": "1"},
        {**row, "SubDownloadLink": "https://dl.org/fp.gz", "SubForeignPartsOnly": "1"},
    ]
    respx.post(ORG).mock(side_effect=[_rpc(LOGIN_OK), _rpc({"status": "200 OK", "data": data})])
    async with httpx.AsyncClient() as http:
        results = await _org(http).search(_movie())
    assert [r.ref["link"] for r in results] == ["https://dl.org/ok.gz"]
    assert results[0].hash_match


@respx.mock
async def test_org_never_searches_an_episode_without_numbers() -> None:
    route = respx.post(ORG).mock(return_value=_rpc(LOGIN_OK))
    async with httpx.AsyncClient() as http:
        assert await _org(http).search(_episode(episode=None)) == []
    assert route.call_count == 0


@respx.mock
async def test_org_spaces_its_calls() -> None:
    respx.post(ORG).mock(side_effect=[_rpc(LOGIN_OK), _rpc({"status": "200 OK", "data": False})])
    began = time.monotonic()
    async with httpx.AsyncClient() as http:
        await OpenSubtitlesOrgSource("u", "p", http, min_interval=0.1).search(_movie())
    assert time.monotonic() - began >= 0.1


@respx.mock
async def test_org_truncated_gzip_download_raises() -> None:
    import gzip

    from homeTheater.subtitles.native.base import InvalidSubtitleError

    respx.get("https://dl.org/t.gz").mock(
        return_value=httpx.Response(200, content=gzip.compress(b"x" * 900)[:-10])
    )
    result = SubtitleResult(
        source="opensubtitles_org",
        lang="en",
        name="t",
        score=0,
        ref={"link": "https://dl.org/t.gz"},
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(InvalidSubtitleError):
            await _org(http).download(result)


# --- opensubtitles.com ------------------------------------------------------


def _hit(file_id: int, **attrs: Any) -> dict[str, Any]:
    return {"attributes": {"language": "en", "files": [{"file_id": file_id}], **attrs}}


def _os_result() -> SubtitleResult:
    return SubtitleResult(source="opensubtitles", lang="en", name="x", score=0, ref={"file_id": 1})


@respx.mock
async def test_com_filters_machine_and_foreign_only_results() -> None:
    body = {
        "data": [
            _hit(1, ai_translated=True),
            _hit(2, machine_translated=True),
            _hit(3, foreign_parts_only=True),
            _hit(4, release="Keep"),
        ]
    }
    respx.get(f"{OS_BASE}/subtitles").mock(return_value=httpx.Response(200, json=body))
    async with httpx.AsyncClient() as http:
        results = await OpenSubtitlesSource("k", http).search(_movie())
    assert [r.ref["file_id"] for r in results] == [4] and results[0].name == "Keep"


@respx.mock
async def test_com_never_searches_an_episode_without_numbers() -> None:
    route = respx.get(f"{OS_BASE}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with httpx.AsyncClient() as http:
        assert await OpenSubtitlesSource("k", http).search(_episode(season=None)) == []
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("code", "expected"),
    [(401, SourceAuthError), (403, SourceAuthError), (429, SourceQuotaError), (500, Exception)],
)
@respx.mock
async def test_com_search_errors_are_typed(code: int, expected: type[Exception]) -> None:
    respx.get(f"{OS_BASE}/subtitles").mock(return_value=httpx.Response(code))
    async with httpx.AsyncClient() as http:
        with pytest.raises(expected):
            await OpenSubtitlesSource("k", http).search(_movie())


@respx.mock
async def test_com_406_on_download_is_quota() -> None:
    respx.post(f"{OS_BASE}/download").mock(return_value=httpx.Response(406, json={"remaining": 0}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceQuotaError):
            await OpenSubtitlesSource("k", http).download(_os_result())


@respx.mock
async def test_com_honours_remaining_zero() -> None:
    respx.post(f"{OS_BASE}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.os/s", "remaining": 0})
    )
    respx.get("https://dl.os/s").mock(return_value=httpx.Response(200, content=b"sub"))
    search = respx.get(f"{OS_BASE}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with httpx.AsyncClient() as http:
        src = OpenSubtitlesSource("k", http)
        assert await src.download(_os_result()) == b"sub"  # the last one still counts
        with pytest.raises(SourceQuotaError):
            await src.search(_movie())
        with pytest.raises(SourceQuotaError):
            await src.download(_os_result())
    assert search.call_count == 0


@respx.mock
async def test_com_login_without_token_is_an_auth_error() -> None:
    respx.post(f"{OS_BASE}/login").mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        src = OpenSubtitlesSource("k", http, username="u", password="p")
        with pytest.raises(SourceAuthError):
            await src.download(_os_result())


@respx.mock
async def test_com_follows_a_redirected_download_link() -> None:
    respx.post(f"{OS_BASE}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.os/a", "remaining": 5})
    )
    respx.get("https://dl.os/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.os/b"})
    )
    respx.get("https://cdn.os/b").mock(return_value=httpx.Response(200, content=b"sub"))
    async with httpx.AsyncClient() as http:
        assert await OpenSubtitlesSource("k", http).download(_os_result()) == b"sub"


# --- ktuvit -----------------------------------------------------------------


def _ktuvit_login(*, cookie: bool = True) -> None:
    respx.get(f"{KTUVIT}/").mock(
        return_value=httpx.Response(200, text="var encryptionSalt = 'ABCDEF0123456789';")
    )
    headers = {"Set-Cookie": "Login=tok"} if cookie else {}
    respx.post(f"{KTUVIT}/Services/MembershipService.svc/Login").mock(
        return_value=httpx.Response(200, json={"d": "no"}, headers=headers)
    )


def _ktuvit_films(films: list[dict[str, Any]]) -> None:
    respx.post(f"{KTUVIT}/Services/ContentProvider.svc/SearchPage_search").mock(
        return_value=httpx.Response(200, json={"d": json.dumps({"Films": films})})
    )
    respx.get(f"{KTUVIT}/MovieInfo.aspx").mock(
        return_value=httpx.Response(200, text='<a data-subtitle-id="S1">x</a>')
    )


async def _ktuvit_film_id(query: SubtitleQuery) -> str | None:
    async with httpx.AsyncClient() as http:
        results = await KtuvitSource("me@x.com", "pw", http).search(query)
    return results[0].ref["film_id"] if results else None


@respx.mock
async def test_ktuvit_skips_a_first_hit_that_is_another_film() -> None:
    _ktuvit_login()
    _ktuvit_films(
        [
            {"ID": "WRONG", "EngName": "The Matrix Reloaded", "Year": "2003"},
            {"ID": "RIGHT", "EngName": "The Matrix!", "Year": "1999"},
        ]
    )
    assert await _ktuvit_film_id(_movie(imdb_id=None, lang="he")) == "RIGHT"


@respx.mock
async def test_ktuvit_returns_nothing_rather_than_guess() -> None:
    _ktuvit_login()
    _ktuvit_films([{"ID": "X", "EngName": "Something Else", "Year": "1999"}])
    assert await _ktuvit_film_id(_movie(imdb_id=None, lang="he")) is None


@respx.mock
async def test_ktuvit_rejects_a_remake_with_the_same_name() -> None:
    _ktuvit_login()
    _ktuvit_films([{"ID": "X", "EngName": "The Matrix", "Year": "2021"}])
    assert await _ktuvit_film_id(_movie(imdb_id=None, lang="he")) is None


@respx.mock
async def test_ktuvit_refuses_a_name_with_no_year() -> None:
    _ktuvit_login()
    _ktuvit_films([{"ID": "X", "EngName": "The Matrix"}])
    assert await _ktuvit_film_id(_movie(imdb_id=None, lang="he")) is None


@respx.mock
async def test_ktuvit_refuses_two_equally_good_hits() -> None:
    _ktuvit_login()
    _ktuvit_films(
        [
            {"ID": "A", "EngName": "The Matrix", "Year": "1999"},
            {"ID": "B", "EngName": "The Matrix", "Year": "2000"},
        ]
    )
    assert await _ktuvit_film_id(_movie(imdb_id=None, lang="he")) is None


@respx.mock
async def test_ktuvit_matches_the_hebrew_name() -> None:
    _ktuvit_login()
    _ktuvit_films([{"ID": "H", "EngName": "", "HebName": "פאודה", "Year": "2015"}])
    query = _movie(imdb_id=None, lang="he", title="פאודה", year=2015)
    assert await _ktuvit_film_id(query) == "H"


@respx.mock
async def test_ktuvit_imdb_id_outranks_the_name() -> None:
    _ktuvit_login()
    link = "https://www.imdb.com/title/tt0133093/"
    _ktuvit_films(
        [
            {"ID": "SAME-NAME", "EngName": "The Matrix", "Year": "1999", "IMDB_Link": "tt9"},
            {"ID": "BY-IMDB", "EngName": "Matrix (heb)", "IMDB_Link": link},
        ]
    )
    assert await _ktuvit_film_id(_movie(lang="he")) == "BY-IMDB"


@respx.mock
async def test_ktuvit_unknown_result_shape_is_an_error_not_silence() -> None:
    _ktuvit_login()
    _ktuvit_films([{"ID": "X", "SomeNewField": "The Matrix"}])
    with pytest.raises(SubtitleSourceError):
        await _ktuvit_film_id(_movie(lang="he"))


@respx.mock
async def test_ktuvit_failed_login_is_an_auth_error() -> None:
    _ktuvit_login(cookie=False)
    with pytest.raises(SourceAuthError):
        await _ktuvit_film_id(_movie(lang="he"))


@respx.mock
async def test_ktuvit_outage_is_an_error_not_no_results() -> None:
    _ktuvit_login()
    respx.post(f"{KTUVIT}/Services/ContentProvider.svc/SearchPage_search").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(SubtitleSourceError):
        await _ktuvit_film_id(_movie(lang="he"))


@respx.mock
async def test_ktuvit_reads_release_names_for_ranking() -> None:
    _ktuvit_login()
    _ktuvit_films([{"ID": "F", "EngName": "The Matrix", "Year": "1999"}])
    html = (
        "<table><tr><td><div>The.Matrix.1999.1080p-GRP</div>\n<span>uploader</span></td>"
        '<td><a data-subtitle-id="S1">d</a></td></tr></table>'
    )
    respx.get(f"{KTUVIT}/MovieInfo.aspx").mock(return_value=httpx.Response(200, text=html))
    async with httpx.AsyncClient() as http:
        results = await KtuvitSource("me@x.com", "pw", http).search(_movie(imdb_id=None, lang="he"))
    assert results[0].name == "The.Matrix.1999.1080p-GRP"


@respx.mock
async def test_ktuvit_download_picks_the_right_episode_from_a_zip() -> None:
    _ktuvit_login()
    respx.post(f"{KTUVIT}/Services/ContentProvider.svc/RequestSubtitleDownload").mock(
        return_value=httpx.Response(200, json={"d": '{"DownloadIdentifier":"D"}'})
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Show.S01E01.srt", b"first")
        zf.writestr("Show.S01E02.srt", b"second")
    respx.get(f"{KTUVIT}/Services/DownloadFile.ashx").mock(
        return_value=httpx.Response(200, content=buf.getvalue())
    )
    result = SubtitleResult(
        source="ktuvit",
        lang="he",
        name="x",
        score=0,
        ref={"film_id": "F", "subtitle_id": "S"},
        season=1,
        episode=2,
    )
    async with httpx.AsyncClient() as http:
        assert await KtuvitSource("me@x.com", "pw", http).download(result) == b"second"
