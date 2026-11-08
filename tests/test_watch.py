"""F8: watch_state merging, Trakt two-way sync, archive suggestions (HTTP mocked)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from homeTheater import watch
from homeTheater.db.models import Candidate, OwnedFile, Title, TitleKind, WatchState
from homeTheater.errors import NotConfiguredError

TRAKT = "https://api.trakt.tv"
T1 = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=30)


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    push: bool = True,
    kodi: bool = False,
) -> Any:
    from homeTheater.config import get_config, loader
    from homeTheater.db import init_db
    from homeTheater.db import session as db_session
    from homeTheater.trakt import save_token

    (tmp_path / "w.yaml").write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'w.db'}'}}\n"
        "trakt:\n"
        "  sync:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        f"    push: {'true' if push else 'false'}\n"
        f"kodi: {{enabled: {'true' if kodi else 'false'}}}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(tmp_path / "w.yaml"))
    monkeypatch.setenv("TRAKT_CLIENT_ID", "cid")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "sec")
    monkeypatch.setattr(watch, "PUSH_PAUSE_S", 0.0)
    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    init_db()
    save_token({"access_token": "acc", "refresh_token": "ref", "expires_in": 7776000})
    return get_config()


def _title(kind: TitleKind, tmdb: int, **kw: Any) -> int:
    from homeTheater.db import session_scope

    with session_scope() as s:
        t = Title(title=f"{kind.value}{tmdb}", kind=kind, tmdb_id=tmdb, **kw)
        s.add(t)
        s.flush()
        return t.id


def _file(title_id: int, kind: TitleKind, name: str, **kw: Any) -> int:
    from homeTheater.db import session_scope

    with session_scope() as s:
        f = OwnedFile(title_id=title_id, kind=kind, path=f"/lib/{name}", **kw)
        s.add(f)
        s.flush()
        return f.id


def _played(owned: int, when: datetime | None = T1, source: str = "kodi", plays: int = 1) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow

    with session_scope() as s:
        watch.merge_watch(s, owned, source, plays, when, utcnow())


def _row(owned: int) -> WatchState | None:
    from homeTheater.db import session_scope

    with session_scope() as s:
        row = s.get(WatchState, owned)
        if row is not None:
            s.expunge(row)
        return row


def _pulls(movies: list[Any] | None = None, shows: list[Any] | None = None) -> None:
    respx.get(f"{TRAKT}/sync/watched/movies").mock(
        return_value=httpx.Response(200, json=movies or [])
    )
    respx.get(f"{TRAKT}/sync/watched/shows").mock(
        return_value=httpx.Response(200, json=shows or [])
    )


def _trakt_movie(tmdb: int, when: datetime, plays: int = 1) -> dict[str, Any]:
    return {
        "plays": plays,
        "last_watched_at": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "movie": {"title": "x", "ids": {"tmdb": tmdb, "imdb": None}},
    }


def _trakt_show(tmdb: int, season: int, *episodes: tuple[int, datetime]) -> dict[str, Any]:
    return {
        "show": {"title": "x", "ids": {"tmdb": tmdb}},
        "seasons": [
            {
                "number": season,
                "episodes": [
                    {
                        "number": number,
                        "plays": 1,
                        "last_watched_at": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    }
                    for number, when in episodes
                ],
            }
        ],
    }


def _history() -> Any:
    return respx.post(f"{TRAKT}/sync/history").mock(
        return_value=httpx.Response(201, json={"added": {"movies": 1, "episodes": 0}})
    )


# --- merge


def test_merge_newest_play_wins_and_the_count_never_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "a")

    _played(owned, T2, "kodi", plays=3)
    _played(owned, T1, "trakt", plays=1)  # older: must not overwrite

    row = _row(owned)
    assert row is not None and row.source == "kodi" and row.play_count == 3
    assert row.last_played_at is not None and row.last_played_at.replace(tzinfo=UTC) == T2

    _played(owned, T2 + timedelta(days=1), "trakt", plays=1)  # newer: takes over

    row = _row(owned)
    assert row is not None and row.source == "trakt" and row.play_count == 3


def test_merge_an_undated_report_never_replaces_a_dated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "a")
    _played(owned, T1, "trakt")

    _played(owned, None, "kodi")

    row = _row(owned)
    assert row is not None and row.source == "trakt" and row.last_played_at is not None


def test_only_the_reporting_source_can_take_a_play_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "a")
    _played(owned, T1, "trakt")

    _played(owned, None, "kodi", plays=0)
    row = _row(owned)
    assert row is not None and row.play_count == 1

    _played(owned, T2, "kodi")
    _played(owned, None, "kodi", plays=0)
    row = _row(owned)
    assert row is not None and row.play_count == 0 and row.last_played_at is None


# --- pull


@respx.mock
async def test_pull_is_kind_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A movie and a series with the same TMDb number are different things."""

    config = _setup(tmp_path, monkeypatch)
    movie = _title(TitleKind.movie, 1396)
    series = _title(TitleKind.series, 1396)
    movie_file = _file(movie, TitleKind.movie, "m")
    episode_file = _file(series, TitleKind.series, "e", season=1, episode=1)
    _pulls(movies=[_trakt_movie(1396, T1)])

    await watch.sync_trakt(config)

    assert _row(movie_file) is not None
    assert _row(episode_file) is None


@respx.mock
async def test_pull_matches_an_episode_only_in_its_own_show_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    movie = _title(TitleKind.movie, 7)
    series = _title(TitleKind.series, 7)
    movie_file = _file(movie, TitleKind.movie, "m")
    e1 = _file(series, TitleKind.series, "e1", season=1, episode=1)
    e2 = _file(series, TitleKind.series, "e2", season=1, episode=2)
    _pulls(shows=[_trakt_show(7, 1, (1, T1))])

    stats = await watch.sync_trakt(config)

    assert stats.updated == 1
    assert _row(e1) is not None and _row(e2) is None and _row(movie_file) is None


@respx.mock
async def test_pull_falls_back_to_imdb_within_the_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    config = _setup(tmp_path, monkeypatch)
    with session_scope() as s:
        t = Title(title="x", kind=TitleKind.movie, imdb_id="tt123")
        s.add(t)
        s.flush()
        title_id = t.id
    owned = _file(title_id, TitleKind.movie, "m")
    entry = _trakt_movie(0, T1)
    entry["movie"]["ids"] = {"tmdb": None, "imdb": "tt123"}
    show = _trakt_show(0, 1, (1, T1))
    show["show"]["ids"] = {"tmdb": None, "imdb": "tt123"}  # same id, other kind
    _pulls(movies=[entry], shows=[show])

    stats = await watch.sync_trakt(config)

    assert _row(owned) is not None
    assert stats.unmatched == 1  # the show found no series


@respx.mock
async def test_pull_does_not_overwrite_a_newer_kodi_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch, push=False)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned, T2)
    _pulls(movies=[_trakt_movie(1, T1)])

    await watch.sync_trakt(config)

    row = _row(owned)
    assert row is not None and row.source == "kodi"
    assert row.last_played_at is not None and row.last_played_at.replace(tzinfo=UTC) == T2


@respx.mock
async def test_a_multi_episode_file_needs_all_its_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    series = _title(TitleKind.series, 7)
    double = _file(series, TitleKind.series, "d", season=1, episode=1, episode_end=2)
    _pulls(shows=[_trakt_show(7, 1, (1, T1))])

    await watch.sync_trakt(config)
    assert _row(double) is None

    _pulls(shows=[_trakt_show(7, 1, (1, T1), (2, T2))])
    await watch.sync_trakt(config)
    row = _row(double)
    assert row is not None and row.last_played_at is not None
    assert row.last_played_at.replace(tzinfo=UTC) == T2


@respx.mock
async def test_watched_follows_pagination_and_rate_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    route = respx.get(f"{TRAKT}/sync/watched/movies")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=[_trakt_movie(1, T1)], headers={"X-Pagination-Page-Count": "2"}),
        httpx.Response(200, json=[_trakt_movie(2, T1)], headers={"X-Pagination-Page-Count": "2"}),
    ]
    respx.get(f"{TRAKT}/sync/watched/shows").mock(return_value=httpx.Response(200, json=[]))

    stats = await watch.sync_trakt(config)

    assert stats.movies_seen == 2


@respx.mock
async def test_a_failed_pull_changes_nothing_and_pushes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned, T1)
    respx.get(f"{TRAKT}/sync/watched/movies").mock(return_value=httpx.Response(500))
    history = _history()

    with pytest.raises(httpx.HTTPStatusError):
        await watch.sync_trakt(config)

    assert history.call_count == 0


# --- push


@respx.mock
async def test_push_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _setup(tmp_path, monkeypatch, push=False)
    assert type(config.trakt.sync).model_fields["push"].default is False
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned, T1)
    _pulls()
    history = _history()

    await watch.sync_trakt(config)

    assert history.call_count == 0


@respx.mock
async def test_push_sends_watched_at_and_runs_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1, imdb_id="tt1"), TitleKind.movie, "m")
    _played(owned, T1)
    _pulls()
    history = _history()

    first = await watch.sync_trakt(config)
    second = await watch.sync_trakt(config)

    assert first.pushed == 1 and second.pushed == 0
    assert history.call_count == 1
    body = json.loads(history.calls[0].request.content)
    assert body == {
        "movies": [{"ids": {"tmdb": 1, "imdb": "tt1"}, "watched_at": "2026-03-01T20:00:00.000Z"}]
    }


@respx.mock
async def test_a_lost_reply_is_not_pushed_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trakt took the play but we never heard: its own list stops a second copy."""

    config = _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned, T1)
    _pulls()
    respx.post(f"{TRAKT}/sync/history").mock(side_effect=httpx.ReadTimeout("slow"))

    stats = await watch.sync_trakt(config)
    assert stats.errors  # noted, and the pull still counted
    row = _row(owned)
    assert row is not None and row.trakt_pushed_at is None

    _pulls(movies=[_trakt_movie(1, T1)])
    history = _history()
    tried = history.call_count  # respx keeps one route per pattern
    stats = await watch.sync_trakt(config)

    assert history.call_count == tried and stats.already_on_trakt == 1
    row = _row(owned)
    assert row is not None and row.trakt_pushed_at is not None


@respx.mock
async def test_a_scrobble_near_the_same_time_counts_as_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned, T1 + timedelta(hours=3))
    _pulls(movies=[_trakt_movie(1, T1)])
    history = _history()

    await watch.sync_trakt(config)

    assert history.call_count == 0


@respx.mock
async def test_a_real_rewatch_is_pushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned, T2)
    _pulls(movies=[_trakt_movie(1, T1)])
    history = _history()

    await watch.sync_trakt(config)

    assert history.call_count == 1


@respx.mock
async def test_plays_that_came_from_trakt_are_never_pushed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned, T1, source="trakt")
    _pulls()
    history = _history()

    await watch.sync_trakt(config)

    assert history.call_count == 0


@respx.mock
async def test_a_play_with_no_date_is_not_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned, None)
    _pulls()
    history = _history()

    stats = await watch.sync_trakt(config)

    assert history.call_count == 0 and stats.push_skipped == 1


@respx.mock
async def test_push_is_batched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, "PUSH_BATCH_FILES", 2)
    for i in range(1, 6):
        _played(_file(_title(TitleKind.movie, i), TitleKind.movie, f"m{i}"), T1)
    _pulls()
    history = _history()

    stats = await watch.sync_trakt(config)

    assert history.call_count == 3 and stats.pushed == 5


@respx.mock
async def test_a_failed_batch_stays_unmarked_for_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, "PUSH_BATCH_FILES", 1)
    files = [_file(_title(TitleKind.movie, i), TitleKind.movie, f"m{i}") for i in (1, 2)]
    for f in files:
        _played(f, T1)
    _pulls()
    respx.post(f"{TRAKT}/sync/history").mock(
        side_effect=[httpx.Response(201, json={"added": {"movies": 1}}), httpx.Response(500)]
    )

    stats = await watch.sync_trakt(config)

    rows = [_row(f) for f in files]
    assert sum(1 for r in rows if r and r.trakt_pushed_at) == 1
    assert stats.errors and "REDACTED" not in stats.errors[0]


@respx.mock
async def test_an_episode_push_names_season_and_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    series = _title(TitleKind.series, 7)
    _played(_file(series, TitleKind.series, "a", season=2, episode=3, episode_end=4), T1)
    _pulls(shows=[_trakt_show(7, 2, (3, T1))])  # E03 is there, E04 isn't
    history = _history()

    await watch.sync_trakt(config)

    assert json.loads(history.calls[0].request.content) == {
        "shows": [
            {
                "ids": {"tmdb": 7},
                "seasons": [
                    {
                        "number": 2,
                        "episodes": [{"number": 4, "watched_at": "2026-03-01T20:00:00.000Z"}],
                    }
                ],
            }
        ]
    }


@respx.mock
async def test_an_expired_token_is_refreshed_for_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    _played(_file(_title(TitleKind.movie, 1), TitleKind.movie, "m"), T1)
    _pulls()
    respx.post(f"{TRAKT}/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 7776000})
    )
    route = respx.post(f"{TRAKT}/sync/history")
    route.side_effect = [httpx.Response(401), httpx.Response(201, json={"added": {"movies": 1}})]

    stats = await watch.sync_trakt(config)

    assert stats.added_on_trakt == 1
    assert route.calls[1].request.headers["authorization"] == "Bearer new"


async def test_off_and_unauthorized_are_quiet_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Setting
    from homeTheater.scheduler import jobs
    from homeTheater.scheduler.catchup import job_intervals

    off = _setup(tmp_path, monkeypatch, enabled=False)
    assert "trakt_sync" not in job_intervals(off.schedule, off)
    with pytest.raises(NotConfiguredError):
        await watch.sync_trakt(off)

    on = _setup(tmp_path, monkeypatch)
    with session_scope() as s:
        s.delete(s.get(Setting, "trakt_token"))
    await jobs.run_trakt_sync_job(on)
    assert "trakt-auth" in jobs.skip_reasons["trakt_sync"]


@respx.mock
async def test_a_trakt_outage_is_a_quiet_skip_for_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import jobs

    config = _setup(tmp_path, monkeypatch)
    respx.get(f"{TRAKT}/sync/watched/movies").mock(return_value=httpx.Response(503))
    sent: list[str] = []

    async def notify(_config: Any, message: str) -> None:
        sent.append(message)

    monkeypatch.setattr(jobs, "notify", notify)

    await jobs.run_trakt_sync_job(config)

    assert sent == []
    assert jobs.skip_reasons["trakt_sync"].startswith("Trakt is unavailable")


# --- archive suggestions


def _series(tmdb: int, status: str = "Ended", episodes: int = 2, **kw: Any) -> int:
    return _title(TitleKind.series, tmdb, series_status=status, episodes_count=episodes, **kw)


def _episodes(title_id: int, *numbers: int) -> list[int]:
    return [
        _file(
            title_id,
            TitleKind.series,
            f"{title_id}-{n}",
            season=1,
            episode=n,
            size_bytes=1000,
        )
        for n in numbers
    ]


def test_fully_watched_ended_series_is_suggested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    t = _series(1)
    for f in _episodes(t, 1, 2):
        _played(f)

    found = watch.archive_suggestions()

    assert [(a.title_id, a.episodes, a.size_bytes) for a in found] == [(t, 2, 2000)]


@pytest.mark.parametrize("status", ["Returning Series", "In Production"])
def test_a_running_series_is_not_suggested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    _setup(tmp_path, monkeypatch)
    for f in _episodes(_series(1, status), 1, 2):
        _played(f)

    assert watch.archive_suggestions() == []


def test_a_partly_watched_series_is_not_suggested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _played(_episodes(_series(1), 1, 2)[0])

    assert watch.archive_suggestions() == []


def test_episodes_not_owned_mean_not_fully_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    for f in _episodes(_series(1, episodes=10), 1, 2):
        _played(f)

    assert watch.archive_suggestions() == []


def test_an_unknown_episode_count_is_not_suggested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    t = _title(TitleKind.series, 1, series_status="Ended")
    for f in _episodes(t, 1):
        _played(f)

    assert watch.archive_suggestions() == []


def test_unwatched_specials_do_not_block_a_suggestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    t = _series(1)
    for f in _episodes(t, 1, 2):
        _played(f)
    _file(t, TitleKind.series, "special", season=0, episode=1)

    assert len(watch.archive_suggestions()) == 1


def test_the_panel_lists_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.api import create_app
    from homeTheater.db import session_scope

    _setup(tmp_path, monkeypatch)
    t = _series(1, followed=True)
    files = _episodes(t, 1, 2)
    for f in files:
        _played(f)

    with TestClient(create_app()) as client:
        html = client.get("/watched").text

    assert "series1" in html and "nothing here is deleted" in html
    with session_scope() as s:
        title = s.get(Title, t)
        assert title is not None and title.followed is True
        assert s.query(OwnedFile).count() == 2
        assert s.query(Candidate).count() == 0


def test_panel_escapes_and_reports_unmapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.api import create_app

    _setup(tmp_path, monkeypatch, kodi=True)
    watch.save_status(
        watch.KODI_STATUS_KEY,
        {
            "seen": 1,
            "mapped": 0,
            "updated": 0,
            "unmapped": 1,
            "unmapped_paths": [{"path": "<script>x</script>", "reason": "r"}],
        },
    )

    with TestClient(create_app()) as client:
        html = client.get("/watched").text

    assert "&lt;script&gt;x" in html
    assert "<script>x" not in html


def test_watch_state_goes_with_its_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scanner bulk-deletes pruned rows with foreign keys on."""

    from sqlalchemy import delete

    from homeTheater.db import session_scope

    _setup(tmp_path, monkeypatch)
    owned = _file(_title(TitleKind.movie, 1), TitleKind.movie, "m")
    _played(owned)

    with session_scope() as s:
        s.execute(delete(OwnedFile).where(OwnedFile.id == owned))

    assert _row(owned) is None
