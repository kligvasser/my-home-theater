"""Full discovery flow + manual add, with TMDb/OMDb mocked via respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.db.models import TitleKind

TMDB = "https://api.themoviedb.org/3"
OMDB = "https://www.omdbapi.com/"


def _details(tmdb_id: int, title: str, imdb: str, votes: int, genres: list[str]) -> dict:
    return {
        "id": tmdb_id,
        "title": title,
        "release_date": "2008-07-16",
        "runtime": 152,
        "vote_average": 8.5,
        "vote_count": votes,
        "popularity": 90.0,
        "genres": [{"id": 1, "name": g} for g in genres],
        "external_ids": {"imdb_id": imdb},
        "imdb_id": imdb,
    }


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


def _mock_common() -> None:
    # TV trending is enabled by default -> return nothing so only movies flow.
    respx.get(f"{TMDB}/trending/tv/week").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    # Owned (603) + a passing title (155) + a failing title (999).
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 603, "title": "The Matrix", "release_date": "1999-03-30"},
                    {"id": 155, "title": "The Dark Knight", "release_date": "2008-07-16"},
                    {"id": 999, "title": "Meh Movie", "release_date": "2010-01-01"},
                ]
            },
        )
    )
    respx.get(f"{TMDB}/movie/155").mock(
        return_value=httpx.Response(
            200, json=_details(155, "The Dark Knight", "tt0468569", 30000, ["Action"])
        )
    )
    respx.get(f"{TMDB}/movie/999").mock(
        return_value=httpx.Response(
            200, json=_details(999, "Meh Movie", "tt9999999", 800, ["Drama"])
        )
    )
    respx.get(OMDB).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=(
                {"Response": "True", "imdbRating": "9.0", "imdbVotes": "2,600,000"}
                if request.url.params.get("i") == "tt0468569"
                else {"Response": "True", "imdbRating": "6.0", "imdbVotes": "5,000"}
            ),
        )
    )


@respx.mock
async def test_discovery_creates_ranked_candidates(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.discovery import run_discovery

    init_db()
    # Seed The Matrix as OWNED with its TMDb id so discovery skips it.
    with session_scope() as s:
        owned = Title(tmdb_id=603, title="The Matrix", year=1999, kind=TitleKind.movie)
        owned.owned_files = [OwnedFile(path="/m.mkv", kind=TitleKind.movie)]
        s.add(owned)

    _mock_common()
    stats = await run_discovery(get_config())

    assert stats.owned_skipped == 1  # The Matrix
    assert stats.considered == 2  # Dark Knight + Meh
    assert stats.created == 1  # only Dark Knight passes
    assert stats.filtered == 1  # Meh fails rating/votes

    from sqlalchemy import select

    from homeTheater.db.models import Candidate

    with session_scope() as s:
        cands = s.scalars(select(Candidate)).all()
        assert len(cands) == 1
        c = cands[0]
        assert "9.0" in c.reason and "2,600,000" in c.reason
        assert "trending" in c.reason
        assert c.score and c.score > 0


@respx.mock
async def test_discovery_dedups_and_skips_live(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.discovery import run_discovery

    init_db()
    _mock_common()
    # First run creates the Dark Knight candidate.
    await run_discovery(get_config())
    # Second run must skip it as a live candidate, creating nothing new.
    stats = await run_discovery(get_config())
    assert stats.live_skipped == 1
    assert stats.created == 0


@respx.mock
async def test_imported_candidate_not_resuggested(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-imported title must not be re-discovered as a fresh candidate,
    even before its owned-file record lands in the catalog (torrent import case)."""
    from sqlalchemy import select

    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateStatus
    from homeTheater.discovery import run_discovery

    init_db()
    _mock_common()
    await run_discovery(get_config())  # creates the candidate
    with session_scope() as s:
        for c in s.scalars(select(Candidate)).all():
            c.status = CandidateStatus.imported  # imported, but no owned_file yet
    stats = await run_discovery(get_config())
    assert stats.created == 0 and stats.live_skipped == 1


@respx.mock
async def test_add_manual(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource
    from homeTheater.discovery.actions import add_manual

    init_db()
    respx.get(f"{TMDB}/movie/27205").mock(
        return_value=httpx.Response(
            200, json=_details(27205, "Inception", "tt1375666", 40000, ["Action"])
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "8.8", "imdbVotes": "2,400,000"}
        )
    )

    cid = await add_manual(get_config(), 27205, TitleKind.movie)
    with session_scope() as s:
        c = s.get(Candidate, cid)
        assert c is not None and c.source == CandidateSource.manual


@respx.mock
@pytest.mark.parametrize("status", [401, 503])
async def test_add_manual_survives_an_omdb_failure(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Ratings are a bonus: a spent quota or outage must not lose the add."""

    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate
    from homeTheater.discovery.actions import add_manual

    init_db()
    respx.get(f"{TMDB}/movie/27205").mock(
        return_value=httpx.Response(
            200, json=_details(27205, "Inception", "tt1375666", 40000, ["Action"])
        )
    )
    body = {"Response": "False", "Error": "Request limit reached!"}
    respx.get(OMDB).mock(return_value=httpx.Response(status, json=body))

    cid = await add_manual(get_config(), 27205, TitleKind.movie)

    with session_scope() as s:
        assert s.get(Candidate, cid) is not None


@respx.mock
async def test_rejected_candidate_not_resuggested(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting a candidate must keep discovery from recreating it forever."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateStatus
    from homeTheater.discovery import run_discovery

    init_db()
    _mock_common()
    await run_discovery(get_config())
    with session_scope() as s:
        cand = s.scalars(select(Candidate)).one()
        cand.status = CandidateStatus.rejected

    stats = await run_discovery(get_config())
    assert stats.rejected_skipped == 1
    assert stats.created == 0
    with session_scope() as s:
        assert len(s.scalars(select(Candidate)).all()) == 1  # no duplicate row


@respx.mock
async def test_movie_and_series_sharing_tmdb_id_coexist(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TMDb movie/tv id namespaces overlap; an owned movie must not shadow a
    series with the same id (nor have its kind flipped by the series upsert)."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.discovery import run_discovery

    init_db()
    with session_scope() as s:
        owned = Title(tmdb_id=1396, title="Some Movie", year=2001, kind=TitleKind.movie)
        owned.owned_files = [OwnedFile(path="/sm.mkv", kind=TitleKind.movie)]
        s.add(owned)

    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.get(f"{TMDB}/trending/tv/week").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20"}]
            },
        )
    )
    respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1396,
                "name": "Breaking Bad",
                "first_air_date": "2008-01-20",
                "episode_run_time": [47],
                "vote_average": 8.9,
                "vote_count": 12000,
                "popularity": 200.0,
                "genres": [{"id": 18, "name": "Drama"}],
                "external_ids": {"imdb_id": "tt0903747", "tvdb_id": 81189},
                "number_of_seasons": 5,
                "number_of_episodes": 62,
                "status": "Ended",
            },
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.5", "imdbVotes": "2,000,000"}
        )
    )

    stats = await run_discovery(get_config())
    assert stats.owned_skipped == 0  # the movie must NOT shadow the series
    assert stats.created == 1

    with session_scope() as s:
        titles = s.scalars(select(Title).where(Title.tmdb_id == 1396)).all()
        kinds = {t.kind for t in titles}
        assert kinds == {TitleKind.movie, TitleKind.series}  # both rows, kinds intact
        series = next(t for t in titles if t.kind == TitleKind.series)
        assert series.seasons_count == 5 and series.series_status == "Ended"


@respx.mock
async def test_candidate_features_snapshot(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate
    from homeTheater.discovery import run_discovery

    init_db()
    _mock_common()
    await run_discovery(get_config())
    with session_scope() as s:
        cand = s.scalars(select(Candidate)).one()
        feats = cand.features
        assert feats is not None
        assert feats["kind"] == "movie"
        assert feats["genres"] == ["Action"]
        assert feats["imdb_rating"] == 9.0
        assert feats["decade"] == 2000


@respx.mock
async def test_discovery_without_omdb_uses_tmdb_fallback(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No OMDb key must not mean zero candidates."""

    monkeypatch.setenv("TMDB_API_KEY", "k")
    monkeypatch.delenv("OMDB_API_KEY", raising=False)
    _reset()
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.discovery import run_discovery

    init_db()
    respx.get(f"{TMDB}/trending/tv/week").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": 155, "title": "The Dark Knight", "release_date": "2008-07-16"}]
            },
        )
    )
    respx.get(f"{TMDB}/movie/155").mock(
        return_value=httpx.Response(
            200, json=_details(155, "The Dark Knight", "tt0468569", 30000, ["Action"])
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 1  # passed via TMDb fallback (8.5 rating, 30k votes)


@respx.mock
async def test_taste_similarity_blended_into_candidates(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an owned library, new candidates carry a taste score + neighbors."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, Genre, OwnedFile, Title

    init_db()
    with session_scope() as s:
        action = Genre(name="Action")
        s.add(action)
        for i, name in enumerate(
            ["Mad Max", "John Wick", "Die Hard", "Heat", "Ronin", "Speed", "Taken", "Salt"]
        ):
            t = Title(
                tmdb_id=2000 + i,
                title=name,
                year=1990 + i,
                kind=TitleKind.movie,
                original_language="en",
            )
            t.genres = [action]
            t.owned_files = [OwnedFile(path=f"/lib/{i}.mkv", kind=TitleKind.movie)]
            s.add(t)

    _mock_common()
    from homeTheater.discovery import run_discovery

    stats = await run_discovery(get_config())
    assert stats.created == 1

    with session_scope() as s:
        cand = s.scalars(select(Candidate).join(Title).where(Title.tmdb_id == 155)).one()
        taste = cand.features["taste"]
        assert taste["score"] > 0  # Dark Knight is Action like the library
        assert len(taste["like"]) > 0
        assert "like:" in cand.reason


# --- isolation (L14) ---


@respx.mock
async def test_one_failing_source_costs_only_its_items(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, JobRun, RunStatus
    from homeTheater.discovery import run_discovery

    init_db()
    respx.get(f"{TMDB}/trending/tv/week").mock(
        side_effect=httpx.ConnectError("api_key=SEKRET down")
    )
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 155, "title": "Dark Knight"}]})
    )
    respx.get(f"{TMDB}/movie/155").mock(
        return_value=httpx.Response(
            200, json=_details(155, "The Dark Knight", "tt0468569", 30000, ["Action"])
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.0", "imdbVotes": "3,000,000"}
        )
    )

    monkeypatch.setattr("homeTheater.metadata.http.asyncio.sleep", _no_sleep)
    stats = await run_discovery(get_config())

    assert stats.created == 1 and stats.sources_failed == 1
    assert not any("SEKRET" in e for e in stats.errors)
    with session_scope() as s:
        assert s.scalars(select(Candidate)).one() is not None
        assert s.scalars(select(JobRun)).one().status is RunStatus.partial


async def _no_sleep(_seconds: float) -> None:
    return None


@respx.mock
async def test_every_source_failing_fails_the_run(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.discovery import run_discovery

    init_db()
    monkeypatch.setattr("homeTheater.metadata.http.asyncio.sleep", _no_sleep)
    monkeypatch.setattr(
        "homeTheater.discovery.service.build_sources", lambda *_a: [_Boom("a"), _Boom("b")]
    )
    with pytest.raises(RuntimeError, match="Discovery failed"):
        await run_discovery(get_config())


class _Boom:
    def __init__(self, name: str) -> None:
        self.name = name

    async def fetch(self, client: object, limit: int) -> list:
        raise RuntimeError("source down")


@respx.mock
async def test_one_bad_item_does_not_lose_the_others(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All items shared one transaction, so one failure rolled back the whole run."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate
    from homeTheater.discovery import run_discovery
    from homeTheater.discovery import service as svc

    init_db()
    _mock_common()
    real_score = svc.score

    def flaky_score(rating: float | None, *args: object, **kwargs: object) -> float:
        if kwargs.get("tmdb_votes") == 30000:
            raise RuntimeError("bad item")
        return real_score(rating, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "score", flaky_score)
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 155, "title": "Dark Knight"}, {"id": 156, "title": "Other"}]},
        )
    )
    for tmdb_id, votes, imdb in ((155, 30000, "tt1"), (156, 30001, "tt2")):
        respx.get(f"{TMDB}/movie/{tmdb_id}").mock(
            return_value=httpx.Response(200, json=_details(tmdb_id, "T", imdb, votes, ["Action"]))
        )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.0", "imdbVotes": "3,000,000"}
        )
    )

    stats = await run_discovery(get_config())

    assert stats.created == 1 and len(stats.errors) == 1
    with session_scope() as s:
        assert len(s.scalars(select(Candidate)).all()) == 1


@respx.mock
async def test_corrupt_model_means_no_model(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.discovery import run_discovery
    from homeTheater.preferences import model_path

    init_db()
    _mock_common()
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 155, "title": "Dark Knight"}]})
    )
    respx.get(f"{TMDB}/movie/155").mock(
        return_value=httpx.Response(
            200, json=_details(155, "The Dark Knight", "tt0468569", 30000, ["Action"])
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.0", "imdbVotes": "3,000,000"}
        )
    )
    path = model_path(get_config())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a model")

    stats = await run_discovery(get_config())
    assert stats.created == 1 and not stats.errors


@respx.mock
async def test_broken_taste_index_means_no_taste(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.discovery import run_discovery

    init_db()
    _mock_common()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sklearn fell over")

    monkeypatch.setattr("homeTheater.taste.build_index", boom)
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 155, "title": "Dark Knight"}]})
    )
    respx.get(f"{TMDB}/movie/155").mock(
        return_value=httpx.Response(
            200, json=_details(155, "The Dark Knight", "tt0468569", 30000, ["Action"])
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.0", "imdbVotes": "3,000,000"}
        )
    )
    stats = await run_discovery(get_config())
    assert stats.created == 1 and not stats.errors


# --- cancelled and failed title-level candidates (contract, L18) ---


async def _discover_dark_knight(monkeypatch: pytest.MonkeyPatch, seed: object) -> list[tuple]:
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, Title
    from homeTheater.discovery import run_discovery

    _env(monkeypatch)
    init_db()
    with session_scope() as s:
        title = Title(tmdb_id=155, title="The Dark Knight", kind=TitleKind.movie)
        s.add(title)
        s.flush()
        seed(s, title.id)  # type: ignore[operator]
    _mock_common()
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 155, "title": "Dark Knight"}]})
    )
    respx.get(f"{TMDB}/movie/155").mock(
        return_value=httpx.Response(
            200, json=_details(155, "The Dark Knight", "tt0468569", 30000, ["Action"])
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.0", "imdbVotes": "3,000,000"}
        )
    )
    stats = await run_discovery(get_config())
    with session_scope() as s:
        return [(c.status.value, stats.considered) for c in s.scalars(select(Candidate))]


@respx.mock
async def test_cancelled_title_blocks_rediscovery(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus

    def seed(s: object, title_id: int) -> None:
        s.add(  # type: ignore[attr-defined]
            Candidate(
                title_id=title_id,
                source=CandidateSource.discovery,
                status=CandidateStatus.cancelled,
            )
        )

    # Skipped before enrichment too: considered stays 0.
    assert await _discover_dark_knight(monkeypatch, seed) == [("cancelled", 0)]


def test_cancelled_is_blocking() -> None:
    from homeTheater.db.models import CandidateStatus
    from homeTheater.discovery.service import BLOCKING_STATUSES

    assert CandidateStatus.cancelled in BLOCKING_STATUSES


@respx.mock
async def test_failed_movie_reuses_its_row_after_backoff(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from homeTheater.db.base import utcnow
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus

    def seed(s: object, title_id: int) -> None:
        s.add(  # type: ignore[attr-defined]
            Candidate(
                title_id=title_id,
                source=CandidateSource.discovery,
                status=CandidateStatus.failed,
                updated_at=utcnow() - timedelta(days=2),
            )
        )

    assert await _discover_dark_knight(monkeypatch, seed) == [("new", 1)]


@respx.mock
async def test_failed_movie_waits_out_its_backoff(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus

    def seed(s: object, title_id: int) -> None:
        s.add(  # type: ignore[attr-defined]
            Candidate(
                title_id=title_id, source=CandidateSource.discovery, status=CandidateStatus.failed
            )
        )

    assert await _discover_dark_knight(monkeypatch, seed) == [("failed", 1)]


@respx.mock
async def test_auto_grab_is_marked_as_no_decision(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The preference model must be able to tell a rule's yes from yours."""

    (tmp_path / "config.yaml").open("a").write("features:\n  auto_approve: true\n")
    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate
    from homeTheater.discovery import run_discovery

    init_db()
    _mock_common()
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 155, "title": "Dark Knight"}]})
    )
    respx.get(f"{TMDB}/movie/155").mock(
        return_value=httpx.Response(
            200, json=_details(155, "The Dark Knight", "tt0468569", 30000, ["Action"])
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.0", "imdbVotes": "3,000,000"}
        )
    )
    await run_discovery(get_config())
    with session_scope() as s:
        assert s.scalars(select(Candidate)).one().features["auto"] is True
