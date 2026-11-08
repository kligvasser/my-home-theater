"""Preference classifier: training, prediction, explanation, API + discovery blend."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeTheater.db.models import CandidateSource, CandidateStatus, TitleKind

TOKEN = {"X-Auth-Token": "test-token"}


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _scifi(i: int) -> dict:
    return {
        "kind": "movie",
        "genres": ["Science Fiction", "Thriller"],
        "keywords": ["dystopia", f"tag{i % 3}"],
        "original_language": "en",
        "decade": 2010,
        "imdb_rating": 7.5 + (i % 3) * 0.3,
        "imdb_votes": 100_000,
        "runtime": 120,
    }


def _romcom(i: int) -> dict:
    return {
        "kind": "movie",
        "genres": ["Romance", "Comedy"],
        "keywords": ["christmas", f"tag{i % 3}"],
        "original_language": "en",
        "decade": 2000,
        "imdb_rating": 7.0 + (i % 3) * 0.3,
        "imdb_votes": 90_000,
        "runtime": 100,
    }


def _seed_decisions(n_each: int = 15) -> None:
    """One title per decision: the classifier keeps one row per title."""

    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, Title

    init_db()
    with session_scope() as s:
        for i in range(n_each):
            for offset, status, feats in (
                (1000, CandidateStatus.approved, _scifi(i)),
                (2000, CandidateStatus.rejected, _romcom(i)),
            ):
                t = Title(tmdb_id=offset + i, title=f"t{offset + i}", kind=TitleKind.movie)
                s.add(t)
                s.flush()
                s.add(
                    Candidate(
                        title_id=t.id,
                        source=CandidateSource.discovery,
                        status=status,
                        features=feats,
                    )
                )


def test_train_refuses_without_labels(config_file: Path) -> None:
    _reset()
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.preferences import predict, train

    init_db()
    stats = train(get_config(), bootstrap=False)
    assert not stats.trained and "not enough decisions" in stats.message
    assert predict(get_config(), _scifi(0)) is None  # no model file


def test_train_learns_the_preference(config_file: Path) -> None:
    _reset()
    _seed_decisions()
    from homeTheater.config import get_config
    from homeTheater.preferences import model_info, predict, train

    cfg = get_config()
    stats = train(cfg, bootstrap=False)
    assert stats.trained and stats.n_positive == 15 and stats.n_negative == 15
    assert stats.auc is not None and stats.auc > 0.9  # cleanly separable

    p_scifi = predict(cfg, _scifi(99))
    p_romcom = predict(cfg, _romcom(99))
    assert p_scifi is not None and p_romcom is not None
    assert p_scifi > 0.7 > 0.3 > p_romcom

    info = model_info(cfg)
    assert info is not None and info.n_positive == 15
    positive_tokens = [tok for tok, _ in info.top_positive]
    assert any("science fiction" in tok for tok in positive_tokens)


def test_train_api_gated_and_reports(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        assert client.post("/api/preferences/train").status_code == 401
        r = client.post("/api/preferences/train", headers=TOKEN)
        assert r.status_code == 200
        assert r.json()["trained"] is False  # empty DB -> no labels

        page = client.get("/insights")
        assert page.status_code == 200 and "Preference model" in page.text


# --- what counts as a label, and how the model is built (L16) ---


def _candidate(
    title_id: int,
    status: CandidateStatus,
    feats: dict,
    *,
    source: CandidateSource = CandidateSource.discovery,
    season: int | None = None,
    reason: str | None = None,
    decided: bool = False,
) -> object:
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import Candidate

    now = utcnow()
    return Candidate(
        title_id=title_id,
        source=source,
        status=status,
        features=feats,
        season=season,
        reason=reason,
        created_at=now,
        decided_at=now if decided else None,
    )


def _one_title(name: str = "t") -> int:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Title

    with session_scope() as s:
        t = Title(title=name, kind=TitleKind.movie)
        s.add(t)
        s.flush()
        return t.id


def _labels() -> list[tuple[int, int, float]]:
    from homeTheater.preferences import _training_rows

    rows, _ = _training_rows(False)
    return [(r.title_id, r.y, r.weight) for r in rows]


def _add(*cands: object) -> None:
    from homeTheater.db import session_scope

    with session_scope() as s:
        s.add_all(cands)  # type: ignore[arg-type]


def test_rule_approvals_are_not_labels(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db

    init_db()
    a, b, c, d = (_one_title(n) for n in "abcd")
    _add(
        _candidate(a, CandidateStatus.imported, {"auto": True}),
        _candidate(b, CandidateStatus.approved, {"x": 1}, reason="following: S02 (auto-grab)"),
        _candidate(c, CandidateStatus.imported, {"x": 1}, decided=True),  # decided at creation
    )
    assert _labels() == []

    # The same instant decision on something you added yourself is a choice.
    _add(_candidate(d, CandidateStatus.imported, {"x": 1}, source=CandidateSource.manual))
    assert [row[0] for row in _labels()] == [d]


def test_rejecting_an_auto_grab_is_a_label(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db

    init_db()
    a = _one_title()
    _add(_candidate(a, CandidateStatus.rejected, {"auto": True}, decided=True))
    assert _labels() == [(a, 0, 1.0)]


def test_cancelled_is_never_a_label(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db

    init_db()
    a = _one_title()
    _add(_candidate(a, CandidateStatus.cancelled, {"x": 1}))
    assert _labels() == []


def test_a_series_counts_once_with_its_newest_decision(config_file: Path) -> None:
    _reset()
    from datetime import timedelta

    from homeTheater.db import init_db
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import Candidate

    init_db()
    a = _one_title()
    now = utcnow()
    earlier = now - timedelta(days=9)

    def row(season: int, status: CandidateStatus, at: object) -> Candidate:
        return Candidate(
            title_id=a,
            season=season,
            source=CandidateSource.discovery,
            status=status,
            features={"s": season},
            created_at=earlier,
            decided_at=at,
        )

    _add(
        row(1, CandidateStatus.imported, now - timedelta(days=5)),
        row(2, CandidateStatus.rejected, now - timedelta(days=1)),
        row(3, CandidateStatus.imported, now - timedelta(days=3)),
    )
    assert _labels() == [(a, 0, 1.0)]


def test_owned_title_with_a_decision_is_not_also_a_weak_positive(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.preferences import _training_rows

    init_db()
    with session_scope() as s:
        decided = Title(title="decided", kind=TitleKind.movie, tmdb_id=1)
        decided.owned_files = [OwnedFile(path="/a", kind=TitleKind.movie)]
        owned = Title(title="owned", kind=TitleKind.movie, tmdb_id=2)
        owned.owned_files = [OwnedFile(path="/b", kind=TitleKind.movie)]
        s.add_all([decided, owned])
        s.flush()
        decided_id, owned_id = decided.id, owned.id
    _add(_candidate(decided_id, CandidateStatus.rejected, {"x": 1}))

    rows, n_bootstrap = _training_rows(True)

    assert n_bootstrap == 1
    assert sorted((r.title_id, r.y) for r in rows) == [(decided_id, 0), (owned_id, 1)]


def test_votes_stay_distinct_and_missing_values_are_flagged() -> None:
    from homeTheater.preferences import featurize

    both = featurize({"imdb_votes": 90, "tmdb_votes": 990})
    assert both["num:imdb_votes_log10"] == 2.0 and both["num:tmdb_votes_log10"] == 3.0
    assert "missing:imdb_votes_log10" not in both

    tmdb_only = featurize({"tmdb_votes": 990, "imdb_rating": None})
    assert "num:imdb_votes_log10" not in tmdb_only  # TMDb votes aren't IMDb votes
    assert tmdb_only["missing:imdb_votes_log10"] == 1.0
    assert tmdb_only["missing:imdb_rating"] == 1.0
    assert "missing:runtime_h" in tmdb_only

    # Zero votes is a value, not a gap.
    assert "num:tmdb_votes_log10" in featurize({"tmdb_votes": 0})
    assert "num:seasons" not in featurize({"kind": "movie", "seasons_count": 3})
    assert "missing:seasons" not in featurize({"kind": "movie"})
    assert "missing:seasons" in featurize({"kind": "series"})


def test_numerics_are_standardized() -> None:
    from homeTheater.preferences import _fit_scales, _scaled

    rows = [{"num:a": 2.0, "tok": 1.0}, {"num:a": 4.0}, {"num:a": 6.0, "num:same": 5.0}]
    scales = _fit_scales(rows)
    assert scales["num:a"][0] == 4.0
    assert scales["num:same"] == (5.0, 1.0)  # no spread: no division by zero
    assert _scaled(rows[0], scales)["num:a"] < 0 and _scaled(rows[1], scales)["num:a"] == 0
    assert _scaled(rows[0], scales)["tok"] == 1.0


def test_probabilities_do_not_saturate_on_unscaled_numerics(config_file: Path) -> None:
    """Large raw numerics (votes, seasons) pinned p_like near 0 or 1."""

    _reset()
    _seed_decisions()
    from homeTheater.config import get_config
    from homeTheater.preferences import predict, train

    cfg = get_config()
    train(cfg, bootstrap=False)
    neutral = {"kind": "movie", "imdb_rating": 7.5, "imdb_votes": 95_000, "runtime": 110}
    p = predict(cfg, neutral)
    assert p is not None and 0.1 < p < 0.9


def test_auc_folds_keep_a_title_together_and_use_the_deployed_recipe(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import preferences as prefs
    from homeTheater.preferences import _Row

    _reset()
    rows = [_Row(prefs.featurize(_scifi(i)), 1, 1.0, i) for i in range(20)]
    rows += [_Row(prefs.featurize(_romcom(i)), 0, 1.0, 100 + i) for i in range(20)]
    rows += [_Row(prefs.featurize(_scifi(i)), 1, prefs.BOOTSTRAP_WEIGHT, 200 + i) for i in range(5)]

    fits: list[list[_Row]] = []
    real_fit = prefs._fit

    def spy(train_rows: list[_Row]) -> dict:
        fits.append(list(train_rows))
        return real_fit(train_rows)

    monkeypatch.setattr(prefs, "_fit", spy)
    auc = prefs._cross_validate(rows)

    assert auc is not None and auc > 0.9
    assert len(fits) == prefs.AUC_FOLDS
    for fit in fits:
        # Weighted bootstrap rows are in every fold: that's what gets deployed.
        assert sum(1 for r in fit if r.weight == prefs.BOOTSTRAP_WEIGHT) == 5
        assert sum(1 for r in fit if r.weight == 1.0) < 40


def test_auc_is_group_split_when_a_title_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater import preferences as prefs
    from homeTheater.preferences import _Row

    rows = [_Row({"x": 1.0, "num:a": float(i)}, i % 2, 1.0, i // 4) for i in range(40)]
    held: list[set[int]] = []
    real_fit = prefs._fit

    def spy(train_rows: list[_Row]) -> dict:
        held.append({r.title_id for r in train_rows})
        return real_fit(train_rows)

    monkeypatch.setattr(prefs, "_fit", spy)
    prefs._cross_validate(rows)
    assert held
    every = {r.title_id for r in rows}
    for train_groups in held:
        assert train_groups != every  # a held-out title left no row behind


def test_bundle_records_the_features_version_and_a_mismatch_is_refused(
    config_file: Path,
) -> None:
    _reset()
    _seed_decisions()
    import joblib

    from homeTheater.config import get_config
    from homeTheater.features import FEATURES_VERSION
    from homeTheater.preferences import (
        _cache,
        model_info,
        model_path,
        predict,
        require_model,
        train,
    )

    cfg = get_config()
    train(cfg, bootstrap=False)
    path = model_path(cfg)
    bundle = joblib.load(path)
    assert bundle["features_version"] == FEATURES_VERSION
    assert predict(cfg, _scifi(1)) is not None

    bundle["features_version"] = FEATURES_VERSION - 1
    joblib.dump(bundle, path)
    _cache.clear()
    assert predict(cfg, _scifi(1)) is None and model_info(cfg) is None

    del bundle["features_version"]  # a model from before the field existed
    joblib.dump(bundle, path)
    _cache.clear()
    assert predict(cfg, _scifi(1)) is None

    from homeTheater.errors import NotConfiguredError

    with pytest.raises(NotConfiguredError):
        require_model(cfg)


def test_corrupt_model_reads_as_no_model(config_file: Path) -> None:
    _reset()
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.preferences import model_info, model_path, predict

    init_db()
    cfg = get_config()
    path = model_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"garbage")
    assert predict(cfg, _scifi(1)) is None and model_info(cfg) is None


def test_prediction_failure_reads_as_no_model(config_file: Path) -> None:
    _reset()
    _seed_decisions()
    import joblib

    from homeTheater.config import get_config
    from homeTheater.preferences import _cache, model_path, predict, train

    cfg = get_config()
    train(cfg, bootstrap=False)
    bundle = joblib.load(model_path(cfg))
    bundle["model"] = None
    joblib.dump(bundle, model_path(cfg))
    _cache.clear()
    assert predict(cfg, _scifi(1)) is None


def test_failed_save_keeps_the_old_model_and_leaves_no_temp_file(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    _seed_decisions()
    import joblib

    from homeTheater.config import get_config
    from homeTheater.preferences import model_path, predict, train

    cfg = get_config()
    train(cfg, bootstrap=False)
    path = model_path(cfg)
    before = path.read_bytes()

    def half_a_file(_bundle: object, target: str) -> None:
        Path(target).write_bytes(b"half")
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(joblib, "dump", half_a_file)
        with pytest.raises(OSError, match="disk full"):
            train(cfg, bootstrap=False)

    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []
    assert predict(cfg, _scifi(1)) is not None


def _bootstrap_weights() -> dict[str, float]:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Title
    from homeTheater.preferences import _training_rows

    rows, _ = _training_rows(True)
    with session_scope() as s:
        names = {t.id: t.title for t in s.query(Title)}
    return {names[r.title_id]: r.weight for r in rows}


def test_owned_and_watched_outweighs_owned_and_unwatched(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import OwnedFile, Title, TitleKind, WatchState
    from homeTheater.preferences import BOOTSTRAP_WEIGHT, WATCHED_BOOTSTRAP_WEIGHT

    init_db()
    with session_scope() as s:
        watched = Title(title="watched", kind=TitleKind.movie, tmdb_id=1)
        watched.owned_files = [OwnedFile(path="/w", kind=TitleKind.movie)]
        unwatched = Title(title="unwatched", kind=TitleKind.movie, tmdb_id=2)
        unwatched.owned_files = [OwnedFile(path="/u", kind=TitleKind.movie)]
        zero = Title(title="zero", kind=TitleKind.movie, tmdb_id=3)
        zero.owned_files = [OwnedFile(path="/z", kind=TitleKind.movie)]
        s.add_all([watched, unwatched, zero])
        s.flush()
        now = utcnow()
        s.add(
            WatchState(
                owned_file_id=watched.owned_files[0].id,
                play_count=1,
                source="kodi",
                updated_at=now,
            )
        )
        s.add(
            WatchState(
                owned_file_id=zero.owned_files[0].id, play_count=0, source="kodi", updated_at=now
            )
        )

    assert WATCHED_BOOTSTRAP_WEIGHT > BOOTSTRAP_WEIGHT
    assert WATCHED_BOOTSTRAP_WEIGHT < 1.0  # stays out of the "real decisions" AUC rows
    assert _bootstrap_weights() == {
        "watched": WATCHED_BOOTSTRAP_WEIGHT,
        "unwatched": BOOTSTRAP_WEIGHT,
        "zero": BOOTSTRAP_WEIGHT,
    }


def test_no_watch_state_table_just_means_nothing_is_watched(config_file: Path) -> None:
    _reset()
    import sqlalchemy as sa

    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title, TitleKind
    from homeTheater.db.session import get_engine
    from homeTheater.preferences import BOOTSTRAP_WEIGHT

    init_db()
    with session_scope() as s:
        t = Title(title="owned", kind=TitleKind.movie, tmdb_id=1)
        t.owned_files = [OwnedFile(path="/o", kind=TitleKind.movie)]
        s.add(t)
    with get_engine().begin() as c:
        c.execute(sa.text("DROP TABLE watch_state"))

    assert _bootstrap_weights() == {"owned": BOOTSTRAP_WEIGHT}
