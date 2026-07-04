"""Native subtitle backend: hash, placement, OpenSubtitles/ktuvit clients, and
the end-to-end sweep — external calls mocked via respx."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.db.models import TitleKind
from homeTheater.subtitles.native.base import SubtitleQuery, opensubtitles_hash

OS_BASE = "https://api.opensubtitles.com/api/v1"
KTUVIT = "https://www.ktuvit.me"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _write_config(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    sources: str = "[opensubtitles]",
    languages: str = "[en]",
    library_base_dir: str | None = None,
) -> None:
    lib = f"  library_base_dir: {library_base_dir}\n" if library_base_dir else ""
    (tmp_path / "subs.yaml").write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'subs.db'}'}}\n"
        "subtitles:\n"
        "  backend: native\n"
        f"  sources: {sources}\n"
        f"  languages: {languages}\n" + lib
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(tmp_path / "subs.yaml"))


def _seed_movie(path: str, *, imdb_id: str = "tt0133093", langs: list[str] | None = None) -> int:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=603, imdb_id=imdb_id, title="The Matrix", year=1999, kind=TitleKind.movie)
        s.add(t)
        s.flush()
        s.add(OwnedFile(title_id=t.id, path=path, kind=TitleKind.movie, subtitle_langs=langs))
        return t.id


# --- hash + placement (pure) ------------------------------------------------


def test_opensubtitles_hash_is_deterministic_and_none_when_small(tmp_path: Path) -> None:
    small = tmp_path / "small.mkv"
    small.write_bytes(b"x" * 1000)
    assert opensubtitles_hash(str(small)) is None  # < 128 KiB

    big = tmp_path / "big.mkv"
    big.write_bytes(b"A" * 200_000)
    h = opensubtitles_hash(str(big))
    assert h is not None and len(h) == 16 and h == opensubtitles_hash(str(big))


def test_placement_maps_unc_and_names_subs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, monkeypatch=monkeypatch, library_base_dir="/mnt/lib")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.subtitles.native.placement import resolve_local_media, subtitle_dest

    cfg = get_config()
    unc = "\\\\MyCloud\\Elements\\Movies\\The Matrix (1999)\\The Matrix (1999).mkv"
    local = resolve_local_media(unc, cfg)
    assert local == "/mnt/lib/Movies/The Matrix (1999)/The Matrix (1999).mkv"
    # local paths pass through unchanged
    assert resolve_local_media("/already/local/x.mkv", cfg) == "/already/local/x.mkv"

    dest = subtitle_dest(local, "he", "Subs")
    assert dest == "/mnt/lib/Movies/The Matrix (1999)/Subs/The Matrix (1999).he.srt"


def test_resolve_local_media_requires_base_for_unc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch=monkeypatch)  # no library_base_dir
    _reset()
    from homeTheater.config import get_config
    from homeTheater.errors import NotConfiguredError
    from homeTheater.subtitles.native.placement import resolve_local_media

    with pytest.raises(NotConfiguredError):
        resolve_local_media("\\\\h\\s\\Movies\\a.mkv", get_config())


# --- OpenSubtitles client ---------------------------------------------------


@respx.mock
async def test_opensubtitles_search_and_download() -> None:
    from homeTheater.subtitles.native.opensubtitles import OpenSubtitlesSource

    search_body = {
        "data": [
            {
                "attributes": {
                    "language": "en",
                    "release": "low",
                    "download_count": 5,
                    "files": [{"file_id": 1}],
                }
            },
            {
                "attributes": {
                    "language": "en",
                    "release": "hashmatch",
                    "moviehash_match": True,
                    "download_count": 1,
                    "files": [{"file_id": 2}],
                }
            },
        ]
    }
    respx.get(f"{OS_BASE}/subtitles").mock(return_value=httpx.Response(200, json=search_body))
    respx.post(f"{OS_BASE}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.os/x.srt"})
    )
    respx.get("https://dl.os/x.srt").mock(return_value=httpx.Response(200, content=b"1\nsub"))

    async with httpx.AsyncClient() as http:
        src = OpenSubtitlesSource("key", http)
        q = SubtitleQuery(
            lang="en",
            kind=TitleKind.movie,
            title="The Matrix",
            year=1999,
            imdb_id="tt0133093",
            release_name="The.Matrix.1999",
            moviehash="abcdef",
        )
        results = await src.search(q)
        best = max(results, key=lambda r: r.score)
        data = await src.download(best)

    assert len(results) == 2
    assert best.ref["file_id"] == 2  # hash match wins over higher download_count
    assert data == b"1\nsub"
    sent = respx.calls.last.request
    assert sent.url == "https://dl.os/x.srt"


@respx.mock
async def test_opensubtitles_search_uses_parent_imdb_for_series() -> None:
    from homeTheater.subtitles.native.opensubtitles import OpenSubtitlesSource

    route = respx.get(f"{OS_BASE}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with httpx.AsyncClient() as http:
        q = SubtitleQuery(
            lang="en",
            kind=TitleKind.series,
            title="Show",
            year=2020,
            imdb_id="tt1234567",
            release_name="Show.S02E03",
            season=2,
            episode=3,
        )
        await OpenSubtitlesSource("key", http).search(q)

    params = route.calls.last.request.url.params
    assert params["parent_imdb_id"] == "1234567"
    assert params["season_number"] == "2" and params["episode_number"] == "3"


# --- opensubtitles.org (XML-RPC) --------------------------------------------


@respx.mock
async def test_opensubtitles_org_login_search_download() -> None:
    import gzip
    import xmlrpc.client

    from homeTheater.subtitles.native.opensubtitles_org import OpenSubtitlesOrgSource

    ORG = "https://api.opensubtitles.org/xml-rpc"

    def _resp(value: object) -> httpx.Response:
        return httpx.Response(200, text=xmlrpc.client.dumps((value,), methodresponse=True))

    search_data = {
        "status": "200 OK",
        "data": [
            {
                "SubFileName": "popular.srt",
                "SubDownloadsCnt": "9000",
                "SubDownloadLink": "https://dl.org/a.gz",
                "SubHearingImpaired": "0",
            },
            {
                "SubFileName": "hashmatch.srt",
                "SubDownloadsCnt": "10",
                "MatchedBy": "moviehash",
                "SubDownloadLink": "https://dl.org/b.gz",
                "SubHearingImpaired": "0",
            },
        ],
    }
    # login then search are two POSTs to the same endpoint
    respx.post(ORG).mock(
        side_effect=[
            _resp({"token": "TOK", "status": "200 OK"}),
            _resp(search_data),
        ]
    )
    respx.get("https://dl.org/b.gz").mock(
        return_value=httpx.Response(200, content=gzip.compress(b"1\nmatched sub"))
    )

    async with httpx.AsyncClient() as http:
        src = OpenSubtitlesOrgSource("user", "pass", http)
        q = SubtitleQuery(
            lang="en",
            kind=TitleKind.movie,
            title="The Matrix",
            year=1999,
            imdb_id="tt0133093",
            release_name="The.Matrix",
            moviehash="abc",
            filesize=123,
        )
        results = await src.search(q)
        best = max(results, key=lambda r: r.score)
        data = await src.download(best)

    assert len(results) == 2
    assert best.ref["link"] == "https://dl.org/b.gz"  # hash match beats popularity
    assert data == b"1\nmatched sub"  # gunzipped


@respx.mock
async def test_opensubtitles_org_no_results_returns_empty() -> None:
    import xmlrpc.client

    from homeTheater.subtitles.native.opensubtitles_org import OpenSubtitlesOrgSource

    ORG = "https://api.opensubtitles.org/xml-rpc"

    def _resp(value: object) -> httpx.Response:
        return httpx.Response(200, text=xmlrpc.client.dumps((value,), methodresponse=True))

    respx.post(ORG).mock(
        side_effect=[
            _resp({"token": "T", "status": "200 OK"}),
            _resp({"status": "200 OK", "data": False}),  # .org returns False, not []
        ]
    )
    async with httpx.AsyncClient() as http:
        q = SubtitleQuery(
            lang="en",
            kind=TitleKind.movie,
            title="Nope",
            year=2000,
            imdb_id="tt9999999",
            release_name="Nope",
        )
        assert await OpenSubtitlesOrgSource("u", "p", http).search(q) == []


# --- ktuvit client ----------------------------------------------------------


@respx.mock
async def test_ktuvit_login_search_download_zip() -> None:
    from homeTheater.subtitles.native.ktuvit import KtuvitSource

    respx.get(f"{KTUVIT}/").mock(
        return_value=httpx.Response(200, text="var encryptionSalt = 'ABCDEF0123456789';")
    )
    respx.post(f"{KTUVIT}/Services/MembershipService.svc/Login").mock(
        return_value=httpx.Response(200, json={"d": "ok"}, headers={"Set-Cookie": "Login=tok"})
    )
    respx.post(f"{KTUVIT}/Services/ContentProvider.svc/SearchPage_search").mock(
        return_value=httpx.Response(200, json={"d": '{"Films":[{"ID":"F1"}]}'})
    )
    respx.get(f"{KTUVIT}/MovieInfo.aspx").mock(
        return_value=httpx.Response(200, text='<a data-subtitle-id="S9">heb</a>')
    )
    respx.post(f"{KTUVIT}/Services/ContentProvider.svc/RequestSubtitleDownload").mock(
        return_value=httpx.Response(200, json={"d": '{"DownloadIdentifier":"D7"}'})
    )
    # a zip payload -> the client must extract the .srt
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("movie.heb.srt", b"1\nshalom")
    respx.get(f"{KTUVIT}/Services/DownloadFile.ashx").mock(
        return_value=httpx.Response(200, content=buf.getvalue())
    )

    async with httpx.AsyncClient() as http:
        src = KtuvitSource("me@x.com", "pw", http)
        assert src.supports("he") and not src.supports("en")
        q = SubtitleQuery(
            lang="he",
            kind=TitleKind.movie,
            title="The Matrix",
            year=1999,
            imdb_id=None,
            release_name="The.Matrix",
        )
        results = await src.search(q)
        data = await src.download(results[0])

    assert results[0].ref == {"film_id": "F1", "subtitle_id": "S9"}
    assert data == b"1\nshalom"


async def test_ktuvit_skips_series_without_episode() -> None:
    from homeTheater.subtitles.native.ktuvit import KtuvitSource

    async with httpx.AsyncClient() as http:
        q = SubtitleQuery(  # no season/episode -> returns [] before any network call
            lang="he",
            kind=TitleKind.series,
            title="Show",
            year=2020,
            imdb_id="tt1",
            release_name="Show",
            season=None,
            episode=None,
        )
        assert await KtuvitSource("e", "p", http).search(q) == []


@respx.mock
async def test_ktuvit_series_episode_lists_subs() -> None:
    from homeTheater.subtitles.native.ktuvit import KtuvitSource

    respx.get(f"{KTUVIT}/").mock(
        return_value=httpx.Response(200, text="var encryptionSalt = 'ABCDEF0123456789';")
    )
    respx.post(f"{KTUVIT}/Services/MembershipService.svc/Login").mock(
        return_value=httpx.Response(200, json={"d": "ok"}, headers={"Set-Cookie": "Login=tok"})
    )
    search = respx.post(f"{KTUVIT}/Services/ContentProvider.svc/SearchPage_search").mock(
        return_value=httpx.Response(200, json={"d": '{"Films":[{"ID":"SER1"}]}'})
    )
    module = respx.get(f"{KTUVIT}/Services/GetModuleAjax.ashx").mock(
        return_value=httpx.Response(200, text='<input data-sub-id="E5"><input data-sub-id="E5">')
    )

    async with httpx.AsyncClient() as http:
        q = SubtitleQuery(
            lang="he",
            kind=TitleKind.series,
            title="Show",
            year=2020,
            imdb_id="tt1",
            release_name="Show.S02E07",
            season=2,
            episode=7,
        )
        results = await KtuvitSource("me@x.com", "pw", http).search(q)

    assert len(results) == 1  # de-duped
    assert results[0].ref == {"film_id": "SER1", "subtitle_id": "E5"}
    import json as _json

    assert _json.loads(search.calls.last.request.content)["request"]["SearchType"] == "1"
    mod_params = module.calls.last.request.url.params
    assert mod_params["SeriesID"] == "SER1"
    assert mod_params["Season"] == "2" and mod_params["Episode"] == "7"


# --- end-to-end sweep -------------------------------------------------------


@respx.mock
async def test_sweep_native_downloads_and_places(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "The Matrix (1999)" / "The Matrix (1999).mkv"
    media.parent.mkdir()
    media.write_bytes(b"M" * 200_000)  # big enough for a moviehash
    _write_config(tmp_path, monkeypatch=monkeypatch, languages="[en]")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "key")
    _reset()
    _seed_movie(str(media))

    respx.get(f"{OS_BASE}/subtitles").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "language": "en",
                            "download_count": 9,
                            "files": [{"file_id": 42}],
                        }
                    }
                ]
            },
        )
    )
    respx.post(f"{OS_BASE}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.os/s.srt"})
    )
    respx.get("https://dl.os/s.srt").mock(
        return_value=httpx.Response(200, content=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    )

    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import OwnedFile, Subtitle
    from homeTheater.subtitles import sweep_subtitles

    stats = await sweep_subtitles(get_config())

    assert stats.considered == 1 and stats.downloaded == 1
    dest = media.parent / "Subs" / "The Matrix (1999).en.srt"
    assert dest.exists() and dest.read_bytes().startswith(b"1\n00:00:01")
    with session_scope() as s:
        sub = s.query(Subtitle).one()
        assert sub.lang == "en" and sub.provider == "opensubtitles" and sub.status == "downloaded"
        of = s.query(OwnedFile).one()
        assert of.subtitle_langs == ["en"]  # coverage updated -> next sweep skips it


@respx.mock
async def test_sweep_native_not_found_leaves_no_sub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "m.mkv"
    media.write_bytes(b"m" * 1000)
    _write_config(tmp_path, monkeypatch=monkeypatch, languages="[en]")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "key")
    _reset()
    _seed_movie(str(media))

    respx.get(f"{OS_BASE}/subtitles").mock(return_value=httpx.Response(200, json={"data": []}))

    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.db.models import OwnedFile, Subtitle
    from homeTheater.subtitles import sweep_subtitles

    stats = await sweep_subtitles(get_config())

    assert stats.downloaded == 0 and stats.not_found == 1
    with session_scope() as s:
        assert s.query(Subtitle).count() == 0
        assert s.query(OwnedFile).one().subtitle_langs in (None, [])
