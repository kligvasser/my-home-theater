"""New-season suggestions for series you already own (library new seasons source)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.db.models import CandidateStatus, TitleKind

TMDB = "https://api.themoviedb.org/3"
OMDB = "https://www.omdbapi.com/"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "k")
    monkeypatch.setenv("OMDB_API_KEY", "k")
    _reset()


def _tv_details(seasons: list[dict]) -> dict:
    return {
        "id": 1396,
        "name": "Breaking Bad",
        "first_air_date": "2008-01-20",
        "episode_run_time": [47],
        "vote_average": 8.9,
        "vote_count": 12000,
        "popularity": 200.0,
        "genres": [{"id": 18, "name": "Drama"}],
        "external_ids": {"imdb_id": "tt0903747", "tvdb_id": 81189},
        "number_of_seasons": len([s for s in seasons if s["season_number"] > 0]),
        "number_of_episodes": 30,
        "status": "Returning Series",
        "seasons": seasons,
    }


def _mock_empty_trending() -> None:
    for media in ("movie", "tv"):
        respx.get(f"{TMDB}/trending/{media}/week").mock(
            return_value=httpx.Response(200, json={"results": []})
        )


def _seed_owned_series(seasons: list[int], episodes: int = 13) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import OwnedFile, Title

    with session_scope() as s:
        t = Title(tmdb_id=1396, title="Breaking Bad", year=2008, kind=TitleKind.series)
        t.owned_files = [
            OwnedFile(
                path=f"/tv/bb/s{n:02d}e{e:02d}.mkv", kind=TitleKind.series, season=n, episode=e
            )
            for n in seasons
            for e in range(1, episodes + 1)
        ]
        s.add(t)


@respx.mock
async def test_new_aired_season_suggested(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owning S1–S2 of a show with an aired S3 yields one season-scoped candidate;
    specials (S0) and unaired seasons are ignored, and a re-run doesn't duplicate."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate
    from homeTheater.discovery import run_discovery

    init_db()
    _seed_owned_series([1, 2])
    _mock_empty_trending()
    respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(
            200,
            json=_tv_details(
                [
                    {"season_number": 0, "episode_count": 3, "air_date": "2009-02-17"},
                    {"season_number": 1, "episode_count": 7, "air_date": "2008-01-20"},
                    {"season_number": 2, "episode_count": 13, "air_date": "2009-03-08"},
                    {"season_number": 3, "episode_count": 13, "air_date": "2010-03-21"},
                    {"season_number": 4, "episode_count": 13, "air_date": "2030-01-01"},
                ]
            ),
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.5", "imdbVotes": "2,000,000"}
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 1

    with session_scope() as s:
        cand = s.scalars(select(Candidate)).one()
        assert cand.season == 3
        assert "new season S03" in cand.reason
        assert "13 episodes" in cand.reason
        assert "you own up to S02" in cand.reason
        assert cand.features["season"] == 3

    # Re-run: the live S3 candidate keeps the season from being re-emitted at all
    # (it must not eat the per-source limit), so nothing is even considered.
    stats = await run_discovery(get_config())
    assert stats.created == 0 and stats.considered == 0


@respx.mock
async def test_rejected_season_blocks_only_that_season(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting S3 must not bury S4: per-(title, season) invariants."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title
    from homeTheater.discovery import run_discovery

    init_db()
    _seed_owned_series([1, 2])
    with session_scope() as s:
        title_id = s.scalars(select(Title.id)).one()
        s.add(
            Candidate(
                title_id=title_id,
                season=3,
                source=CandidateSource.discovery,
                status=CandidateStatus.rejected,
            )
        )

    _mock_empty_trending()
    respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(
            200,
            json=_tv_details(
                [
                    {"season_number": 1, "episode_count": 7, "air_date": "2008-01-20"},
                    {"season_number": 2, "episode_count": 13, "air_date": "2009-03-08"},
                    {"season_number": 3, "episode_count": 13, "air_date": "2010-03-21"},
                    {"season_number": 4, "episode_count": 13, "air_date": "2011-07-17"},
                ]
            ),
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.5", "imdbVotes": "2,000,000"}
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 1  # S4 only; rejected S3 stays rejected
    with session_scope() as s:
        seasons = {c.season: c.status for c in s.scalars(select(Candidate)).all()}
        assert seasons[3] == CandidateStatus.rejected
        assert seasons[4] == CandidateStatus.new


@respx.mock
async def test_series_without_new_seasons_makes_no_details_call_noise(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully-owned show produces no candidates (and no crash on cache hits)."""

    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.discovery import run_discovery

    init_db()
    _seed_owned_series([1, 2, 3])
    _mock_empty_trending()
    respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(
            200,
            json=_tv_details(
                [
                    {"season_number": 1, "episode_count": 7, "air_date": "2008-01-20"},
                    {"season_number": 2, "episode_count": 13, "air_date": "2009-03-08"},
                    {"season_number": 3, "episode_count": 13, "air_date": "2010-03-21"},
                ]
            ),
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 0


def test_build_query_targets_season() -> None:
    from homeTheater.acquisition.torrent.select import build_query

    assert build_query("Breaking Bad", 2008, TitleKind.series, season=3) == "Breaking Bad S03"
    assert build_query("Breaking Bad", 2008, TitleKind.series) == "Breaking Bad"
    assert build_query("Heat", 1995, TitleKind.movie, season=None) == "Heat 1995"


@respx.mock
async def test_followed_series_auto_grabs_new_season(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A followed series with no files yet yields an *approved* (auto-grab)
    season candidate for each aired season — not a 'new' suggestion."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateStatus, Title
    from homeTheater.discovery import run_discovery

    init_db()
    # Follow Silo, owning nothing of it.
    with session_scope() as s:
        s.add(Title(tmdb_id=1396, title="Silo", year=2023, kind=TitleKind.series, followed=True))

    _mock_empty_trending()
    respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(
            200,
            json=_tv_details(
                [
                    {"season_number": 1, "episode_count": 10, "air_date": "2023-05-05"},
                    {"season_number": 2, "episode_count": 10, "air_date": "2024-11-15"},
                    {"season_number": 3, "episode_count": 10, "air_date": "2099-01-01"},  # unaired
                ]
            ),
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "8.9", "imdbVotes": "300000"}
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 2  # S1 + S2 aired; S3 not yet

    with session_scope() as s:
        cands = s.scalars(select(Candidate).join(Title).where(Title.tmdb_id == 1396)).all()
        assert {c.season for c in cands} == {1, 2}
        assert all(c.status == CandidateStatus.approved for c in cands)  # auto-grab
        assert all("following" in (c.reason or "") for c in cands)


# --- freshness and partial seasons (L10) ---


def _seasons(*counts: int) -> list[dict]:
    return [
        {"season_number": n, "episode_count": c, "air_date": "2020-01-01"}
        for n, c in enumerate(counts, 1)
    ]


def _mock_show(seasons: list[dict], **extra: object) -> respx.Route:
    route = respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(200, json={**_tv_details(seasons), **extra})
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.5", "imdbVotes": "2,000,000"}
        )
    )
    return route


async def _discover() -> object:
    from homeTheater.config import get_config
    from homeTheater.discovery import run_discovery

    return await run_discovery(get_config())


def _candidates() -> list[tuple[int | None, str, str]]:
    from sqlalchemy import select

    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate

    with session_scope() as s:
        return [
            (c.season, c.status.value, c.reason or "")
            for c in s.scalars(select(Candidate).order_by(Candidate.id))
        ]


def _age_cache(days: float) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import MetadataCache

    with session_scope() as s:
        s.execute(update(MetadataCache).values(fetched_at=utcnow() - timedelta(days=days)))


@respx.mock
async def test_season_list_is_at_most_a_day_stale(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new season must not wait out the 14-day metadata cache."""

    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=7)
    _mock_empty_trending()
    route = _mock_show(_seasons(7))

    await _discover()
    assert _candidates() == [] and route.call_count == 1

    await _discover()  # same day: served from the cache
    assert route.call_count == 1

    route.mock(return_value=httpx.Response(200, json=_tv_details(_seasons(7, 8))))
    _age_cache(days=1.5)
    await _discover()
    assert [c[0] for c in _candidates()] == [2]


@respx.mock
async def test_partial_newest_season_gets_topped_up(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1, 2], episodes=5)
    _mock_empty_trending()
    _mock_show(_seasons(13, 10))

    await _discover()

    # Only the newest season: older holes are a choice you've had years to make.
    [(season, status, reason)] = _candidates()
    assert season == 2 and status == "new"
    assert "5 of 10 episodes" in reason


@respx.mock
async def test_unaired_episodes_are_not_missing(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TMDb's episode_count includes announced episodes."""

    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=5)
    _mock_empty_trending()
    _mock_show(_seasons(10), last_episode_to_air={"season_number": 1, "episode_number": 5})

    await _discover()
    assert _candidates() == []


@respx.mock
async def test_multi_episode_files_count_as_their_whole_range(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=1396, title="Breaking Bad", kind=TitleKind.series)
        t.owned_files = [
            OwnedFile(path="/a.mkv", kind=TitleKind.series, season=1, episode=1, episode_end=2),
            OwnedFile(path="/b.mkv", kind=TitleKind.series, season=1, episode=3),
        ]
        s.add(t)
    _mock_empty_trending()
    _mock_show(_seasons(3))

    await _discover()
    assert _candidates() == []


@respx.mock
async def test_rejected_partial_season_stays_rejected(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=5)
    _seed_candidate(season=1, status=CandidateStatus.rejected)
    _mock_empty_trending()
    _mock_show(_seasons(10))

    await _discover()
    assert [c[1] for c in _candidates()] == ["rejected"]


@respx.mock
async def test_imported_season_is_revisited_only_when_more_has_aired(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Imported at 8 episodes and the show ended on a missing one: leave it. A
    count that has grown since is new."""

    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=7)
    _seed_candidate(
        season=1, status=CandidateStatus.imported, features={"season_episodes": 8, "season": 1}
    )
    _mock_empty_trending()
    route = _mock_show(_seasons(8))

    await _discover()
    assert [c[1] for c in _candidates()] == ["imported"]

    route.mock(return_value=httpx.Response(200, json=_tv_details(_seasons(10))))
    _age_cache(days=2)
    await _discover()
    assert [c[1] for c in _candidates()] == ["imported", "new"]


@respx.mock
async def test_files_without_a_season_number_count(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """season=NULL files used to make the show invisible to this source."""

    _env(monkeypatch)
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=1396, title="Breaking Bad", kind=TitleKind.series)
        t.owned_files = [
            OwnedFile(path=f"/{e}.mkv", kind=TitleKind.series, season=None, episode=e)
            for e in range(1, 4)
        ]
        s.add(t)
    _mock_empty_trending()
    _mock_show(_seasons(3, 5))

    await _discover()
    assert [c[0] for c in _candidates()] == [2]


@respx.mock
async def test_absolute_episode_numbers_map_onto_seasons(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anime numbered 1..30 across two seasons: S2 is owned, S3 is what's new."""

    _env(monkeypatch)
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=1396, title="Breaking Bad", kind=TitleKind.series)
        t.owned_files = [
            OwnedFile(path=f"/{e}.mkv", kind=TitleKind.series, season=None, episode=e)
            for e in range(1, 31)
        ]
        s.add(t)
    _mock_empty_trending()
    _mock_show(_seasons(13, 17, 10))

    await _discover()
    assert [c[0] for c in _candidates()] == [3]


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CandidateStatus.approved, []),  # about to expand into every season
        (CandidateStatus.queued, []),
        (CandidateStatus.imported, [2]),  # an old import can't cover a later season
        (CandidateStatus.rejected, [2]),  # season decisions stay independent
    ],
)
async def test_whole_show_candidate_in_flight_covers_its_seasons(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: CandidateStatus,
    expected: list[int],
) -> None:
    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=7)
    _seed_candidate(season=None, status=status)
    _mock_empty_trending()
    _mock_show(_seasons(7, 8))

    stats = await _discover()
    assert [c[0] for c in _candidates() if c[0] is not None] == expected
    assert stats.considered == len(expected)  # not emitted, so no limit is eaten


# --- failed seasons reuse their row (L18) ---


def _seed_candidate(
    *,
    season: int | None,
    status: CandidateStatus,
    features: dict | None = None,
    failed_days_ago: float = 0,
) -> int:
    from datetime import timedelta

    from sqlalchemy import select, update

    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import Candidate, CandidateSource, Title

    with session_scope() as s:
        title = s.scalars(select(Title).where(Title.tmdb_id == 1396)).one()
        cand = Candidate(
            title_id=title.id,
            season=season,
            source=CandidateSource.discovery,
            status=status,
            features=features or {},
        )
        s.add(cand)
        s.flush()
        cand_id = cand.id
        if failed_days_ago:
            s.execute(
                update(Candidate)
                .where(Candidate.id == cand_id)
                .values(updated_at=utcnow() - timedelta(days=failed_days_ago))
            )
        return cand_id


def _row_count() -> int:
    from sqlalchemy import func, select

    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate

    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(Candidate)) or 0


@respx.mock
async def test_failed_season_waits_out_its_backoff(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=7)
    _seed_candidate(season=2, status=CandidateStatus.failed, failed_days_ago=0.5)
    _mock_empty_trending()
    _mock_show(_seasons(7, 8))

    stats = await _discover()
    assert _candidates()[0][1] == "failed" and _row_count() == 1
    assert stats.considered == 0  # not emitted, so it doesn't eat the source limit


@respx.mock
async def test_failed_season_reuses_its_row_instead_of_piling_up(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate

    init_db()
    _seed_owned_series([1], episodes=7)
    cand_id = _seed_candidate(season=2, status=CandidateStatus.failed, failed_days_ago=2)
    _mock_empty_trending()
    _mock_show(_seasons(7, 8))

    stats = await _discover()

    assert _row_count() == 1 and stats.created == 0 and stats.retried == 1
    with session_scope() as s:
        cand = s.scalars(select(Candidate)).one()
        assert cand.id == cand_id and cand.status is CandidateStatus.new
        assert cand.features["discovery_retries"] == 1
        assert cand.features["version"]  # refreshed like a new candidate


@respx.mock
async def test_backoff_doubles_with_each_retry(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=7)
    _seed_candidate(
        season=2,
        status=CandidateStatus.failed,
        features={"discovery_retries": 2},
        failed_days_ago=3,
    )
    _mock_empty_trending()
    _mock_show(_seasons(7, 8))

    await _discover()  # third failure waits 4 days
    assert [c[1] for c in _candidates()] == ["failed"]


@respx.mock
async def test_existing_pile_is_left_alone(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the newest failed row comes back; history isn't deleted."""

    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=7)
    for _ in range(3):
        _seed_candidate(season=2, status=CandidateStatus.failed, failed_days_ago=30)
    _mock_empty_trending()
    _mock_show(_seasons(7, 8))

    await _discover()
    assert [c[1] for c in _candidates()] == ["failed", "failed", "new"]


@respx.mock
async def test_cancelled_season_is_not_resuggested(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.db import init_db

    init_db()
    _seed_owned_series([1], episodes=7)
    _seed_candidate(season=2, status=CandidateStatus.cancelled)
    _mock_empty_trending()
    _mock_show(_seasons(7, 8))

    stats = await _discover()
    assert [c[1] for c in _candidates()] == ["cancelled"] and stats.considered == 0


def test_in_flight_whole_show_candidate_blocks_a_season_at_write_time(config_file: Path) -> None:
    """The write-time guard: one can appear between the fetch and the insert."""

    _reset()
    from sqlalchemy import select

    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title
    from homeTheater.discovery.service import _blocked_in_db

    init_db()
    _seed_owned_series([1], episodes=7)
    _seed_candidate(season=None, status=CandidateStatus.queued)
    with session_scope() as s:
        title_id = s.scalars(select(Title.id)).one()
        assert _blocked_in_db(s, title_id, 2) == "candidate already queued"
