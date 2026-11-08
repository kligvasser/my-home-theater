"""Read-only dashboard: queries, HTML pages, and JSON API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeTheater.dashboard import human_size


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _seed() -> None:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Genre, OwnedFile, Title, TitleKind

    init_db()
    with session_scope() as s:
        matrix = Title(
            title="The Matrix",
            year=1999,
            kind=TitleKind.movie,
            imdb_rating=8.7,
            imdb_votes=1_900_000,
        )
        matrix.genres = [Genre(name="Action"), Genre(name="Sci-Fi")]
        matrix.owned_files = [
            OwnedFile(
                path="/Movies/matrix.mkv",
                kind=TitleKind.movie,
                resolution="1080p",
                size_bytes=8_000_000_000,
                subtitle_langs=["he", "en"],
            )
        ]
        bb = Title(title="Breaking Bad", year=2008, kind=TitleKind.series)
        bb.owned_files = [
            OwnedFile(
                path="/TV/bb.mkv",
                kind=TitleKind.series,
                resolution="720p",
                size_bytes=2_000_000_000,
                subtitle_langs=[],
            )
        ]
        s.add_all([matrix, bb])


def test_human_size() -> None:
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(8_000_000_000).endswith("GB")


def test_get_stats(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.dashboard import get_stats

    stats = get_stats()
    assert stats.total_titles == 2
    assert stats.movies == 1 and stats.series == 1
    assert stats.files == 2
    assert stats.total_size_bytes == 10_000_000_000
    assert ("1080p", 1) in stats.resolutions and ("720p", 1) in stats.resolutions
    assert ("Action", 1) in stats.genres
    assert dict(stats.decades) == {1990: 1, 2000: 1}
    # Only The Matrix has a Hebrew sidecar -> 1 of 2 owned titles.
    assert stats.coverage.covered == 1 and stats.coverage.total == 2
    assert stats.coverage.pct == 50.0


def test_list_titles_search_and_filter(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.dashboard import list_titles

    rows, total = list_titles(q="matrix")
    assert total == 1 and rows[0].title == "The Matrix"
    assert rows[0].has_sub is True
    assert set(rows[0].genres) == {"Action", "Sci-Fi"}

    rows, total = list_titles(kind="series")
    assert total == 1 and rows[0].title == "Breaking Bad"
    assert rows[0].has_sub is False


def test_list_titles_sorting(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.dashboard import list_titles

    def order(sort: str, direction: str) -> list[str]:
        rows, _ = list_titles(sort=sort, direction=direction)
        return [r.title for r in rows]

    assert order("title", "asc") == ["Breaking Bad", "The Matrix"]
    assert order("year", "asc") == ["The Matrix", "Breaking Bad"]  # 1999, 2008
    assert order("year", "desc") == ["Breaking Bad", "The Matrix"]
    # rating: Breaking Bad has no rating -> always last, both directions
    assert order("rating", "desc") == ["The Matrix", "Breaking Bad"]
    assert order("res", "desc") == ["The Matrix", "Breaking Bad"]  # 1080p > 720p
    assert order("subs", "desc") == ["The Matrix", "Breaking Bad"]  # he+en vs none


def test_html_pages_render(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "Library at a glance" in r.text
        assert "Recently added" in r.text  # poster wall shows the owned title
        assert "The Matrix" in r.text

        r = client.get("/library?q=matrix")
        assert r.status_code == 200
        assert "The Matrix" in r.text

        r = client.get("/runs")
        assert r.status_code == 200


def test_unknown_status_filter_is_handled(config_file: Path) -> None:
    """Bad ?status= values must not 500: HTML falls back, JSON API rejects."""

    _reset()
    _seed()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        r = client.get("/candidates", params={"status": "bogus"})
        assert r.status_code == 200  # falls back to the "new" queue

        r = client.get("/api/candidates", params={"status": "bogus"})
        assert r.status_code == 422  # validated against CandidateStatus


def test_json_api(config_file: Path) -> None:
    _reset()
    _seed()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        stats = client.get("/api/stats").json()
        assert stats["total_titles"] == 2
        assert stats["coverage"]["pct"] == 50.0

        titles = client.get("/api/titles", params={"kind": "movie"}).json()
        assert titles["total"] == 1
        assert titles["items"][0]["title"] == "The Matrix"


def test_library_excludes_unowned_candidate_titles(config_file: Path) -> None:
    _reset()
    from homeTheater.dashboard import get_stats, list_titles
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title, TitleKind

    init_db()
    with session_scope() as s:
        owned = Title(title="Owned Movie", year=2020, kind=TitleKind.movie)
        owned.owned_files = [OwnedFile(path="/Movies/o.mkv", kind=TitleKind.movie)]
        # a discovery candidate: a catalog Title with NO file on disk
        s.add_all([owned, Title(title="Unreleased 2026", year=2026, kind=TitleKind.movie)])

    rows, total = list_titles()
    assert total == 1 and [r.title for r in rows] == ["Owned Movie"]
    # stats count owned only, matching the library
    assert get_stats().movies == 1 and get_stats().total_titles == 1


def test_require_auth_locks_whole_site(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With DASHBOARD_REQUIRE_AUTH, even read pages need the token; static and
    health stay open, and the token (header or Basic) grants access."""
    import base64

    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    monkeypatch.setenv("DASHBOARD_REQUIRE_AUTH", "true")
    _reset()

    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        # A read page is now gated.
        r = client.get("/library")
        assert r.status_code == 401 and "Basic" in r.headers.get("www-authenticate", "")
        # Health + static remain reachable for probes/assets.
        assert client.get("/health").status_code == 200
        # Header token works (dashboard JS path).
        assert client.get("/library", headers={"X-Auth-Token": "tok"}).status_code == 200
        # HTTP Basic (password = token) works (browser navigation).
        basic = base64.b64encode(b"x:tok").decode()
        assert (
            client.get("/library", headers={"Authorization": f"Basic {basic}"}).status_code == 200
        )
        # Wrong token rejected.
        assert client.get("/library", headers={"X-Auth-Token": "nope"}).status_code == 401


def test_reads_open_by_default(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the flag, read pages stay open (default LAN behavior)."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    monkeypatch.delenv("DASHBOARD_REQUIRE_AUTH", raising=False)
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        assert client.get("/library").status_code == 200


def test_sync_endpoint_reports_already_running(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual sync while one is in progress reports it, not a false 'started'."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset()
    from homeTheater.api import create_app
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.locks import job_lock

    init_db()
    with TestClient(create_app()) as client:
        with job_lock(get_config(), "sync"):  # simulate an in-progress sync
            r = client.post("/api/pipeline/sync", headers={"X-Auth-Token": "tok"})
        assert r.status_code == 200
        body = r.json()
        assert body["started"] is False and "already running" in body["message"]


# --- Runs: stats read as text, not as a JSON dump ---------------------------


def test_subtitle_sweep_stats_read_sensibly() -> None:
    from homeTheater.dashboard.queries import RunStat, _pretty_run_stats

    summary, errors = _pretty_run_stats(
        {
            "considered": 12,
            "downloaded": 0,
            "adopted": 3,
            "not_found": 1,
            "invalid": 2,
            "failed": 1,
            "cooling_down": 4,
            "absent": 5,
            "unidentified": 6,
            "source_down": 7,
            "unsupported": 8,
            "disabled_sources": {"ktuvit": "login failed"},
            "errors": ["one", "two"],
        }
    )
    assert errors == ["one", "two"]
    assert RunStat("3 adopted from disk") in summary
    assert RunStat("4 cooling down") in summary
    assert RunStat("6 unidentified") in summary
    assert RunStat("2 invalid only", True) in summary
    assert RunStat("1 failed", True) in summary
    assert RunStat("5 files missing", True) in summary
    assert RunStat("7 source down", True) in summary
    assert RunStat("8 no source for language", True) in summary
    assert RunStat("ktuvit disabled: login failed", True) in summary
    assert not any("downloaded" in s.text for s in summary)  # zeros are noise


def test_unknown_run_stats_still_appear() -> None:
    from homeTheater.dashboard.queries import _pretty_run_stats

    summary, _ = _pretty_run_stats(
        {"titles_new": 4, "dry_run": True, "skipped": [1, 2], "mode": "fast", "none": None}
    )
    assert [s.text for s in summary] == ["4 titles new", "dry run", "2 skipped", "mode: fast"]


def _seed_run(status: object, stats: dict[str, object] | None) -> None:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import JobRun

    init_db()
    with session_scope() as s:
        s.add(JobRun(kind="subtitles", status=status, started_at=utcnow(), stats=stats))


def test_runs_page_renders_a_partial_run(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db.models import RunStatus

    _seed_run(
        RunStatus.partial,
        {"adopted": 2, "disabled_sources": {"ktuvit": "<b>down</b>"}, "errors": ["boom <i>"]},
    )
    with TestClient(create_app()) as client:
        html = client.get("/runs").text
    assert '<span class="pill partial">partial</span>' in html
    assert "2 adopted from disk" in html
    assert "1 error<" in html and "boom &lt;i&gt;" in html
    assert "ktuvit disabled: &lt;b&gt;down&lt;/b&gt;" in html
    assert '{"adopted"' not in html


def test_run_with_no_stats_shows_a_dash(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db.models import RunStatus

    _seed_run(RunStatus.success, None)
    with TestClient(create_app()) as client:
        assert '<td class="small run-stats">—' in client.get("/runs").text


# --- Following: what unfollowing would stop ---------------------------------


def _seed_followed() -> None:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import (
        Candidate,
        CandidateSource,
        CandidateStatus,
        Title,
        TitleKind,
    )

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=9, title='Show "<1>"', kind=TitleKind.series, followed=True)
        s.add(t)
        s.flush()
        for st in (
            CandidateStatus.queued,
            CandidateStatus.downloading,
            CandidateStatus.approved,
            CandidateStatus.imported,
            CandidateStatus.new,
        ):
            s.add(Candidate(title_id=t.id, source=CandidateSource.discovery, status=st))


def test_followed_row_counts_the_grabs_unfollow_stops(config_file: Path) -> None:
    _reset()
    _seed_followed()
    from homeTheater.dashboard import list_followed

    assert list_followed()[0].active == 3


@pytest.mark.parametrize("path", ["/", "/candidates"])
def test_unfollow_button_has_a_name_and_the_count(config_file: Path, path: str) -> None:
    _reset()
    _seed_followed()
    from homeTheater.api import create_app

    with TestClient(create_app()) as client:
        html = client.get(path).text
    assert 'aria-label="Stop following Show &#34;&lt;1&gt;&#34;"' in html
    assert 'data-name="Show &#34;&lt;1&gt;&#34;"' in html
    assert 'data-active="3"' in html


def test_cancelled_candidates_have_a_page(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title, TitleKind

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=4, title="Stopped One", kind=TitleKind.movie)
        s.add(t)
        s.flush()
        s.add(
            Candidate(
                title_id=t.id, source=CandidateSource.manual, status=CandidateStatus.cancelled
            )
        )
    with TestClient(create_app()) as client:
        html = client.get("/candidates?status=cancelled").text
    assert "Stopped One" in html and '<span class="pill">cancelled</span>' in html


def test_status_filters_cover_every_candidate_status(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db
    from homeTheater.db.models import CandidateStatus

    init_db()
    with TestClient(create_app()) as client:
        cand = client.get("/candidates").text
    for status in CandidateStatus:
        assert f"/candidates?status={status}&kind=" in cand.replace("&amp;", "&")
