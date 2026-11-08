"""Trained preference classifier (the plan's 'taste model, supervised half').

Training data = candidate feature snapshots (``Candidate.features``, frozen at
decision time) labeled by what you did with them:

* positive — approved / queued / downloading / imported
* negative — rejected

Only human decisions count: a candidate approved by rule (followed show,
``auto_approve``) says nothing about taste, and ``cancelled`` means "stop", not
"dislike", and a quality upgrade is about a file, not the movie. Each title is one
row, carrying its newest decision, because the seasons of a show share their
features and would otherwise count as several votes.

Optionally (``bootstrap``), owned titles nobody decided on are added as weak
positives with a low sample weight so the model has signal before many decisions
exist; one you've watched (``watch_state``) weighs more than one just sitting there.
The model is logistic regression over one-hot content tokens + scaled numeric
features, with flags for what's missing — small, fast, and *explainable*: coefficients
say which tokens push toward approve.

The model rides ``data/preference_model.joblib`` next to the SQLite DB and is
blended into discovery scoring only when a usable one exists
(``taste.model_weight``): one that's corrupt or was trained on another
``FEATURES_VERSION`` counts as no model until the next retrain.
kNN similarity (homeTheater.taste) answers "is it like what I own?"; this model
answers the sharper question "is it like what I *approve*?".
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import selectinload

from .config import AppConfig
from .db.base import utcnow
from .db.models import Candidate, CandidateSource, CandidateStatus, OwnedFile, Title
from .db.session import session_scope
from .errors import NotConfiguredError, redact_exc
from .features import FEATURES_VERSION, extract_features
from .logging_setup import get_logger
from .taste import _pretty_token, tokens_from_features
from .watch import watched_title_ids

log = get_logger(__name__)

MODEL_FILENAME = "preference_model.joblib"

POSITIVE_STATUSES = (
    CandidateStatus.approved,
    CandidateStatus.queued,
    CandidateStatus.downloading,
    CandidateStatus.imported,
)
MIN_LABELS = 20
MIN_PER_CLASS = 5
BOOTSTRAP_WEIGHT = 0.25  # owned titles are weak positives vs. real decisions
WATCHED_BOOTSTRAP_WEIGHT = 0.5  # owned and watched says more; still not a decision
# A candidate decided this soon after it was created was decided by a rule. Older
# rows predate the features["auto"] marker, so this is how they're recognized.
AUTO_DECISION_WINDOW = timedelta(seconds=5)
AUC_FOLDS = 3
AUC_MIN_ROWS = 30
AUC_MIN_PER_CLASS = 10

_cache: dict[str, Any] = {}  # path -> (mtime, bundle, or None if unusable)


@dataclass(frozen=True, slots=True)
class _Row:
    x: dict[str, float]
    y: int
    weight: float
    title_id: int


@dataclass
class TrainStats:
    trained: bool = False
    n_positive: int = 0
    n_negative: int = 0
    n_bootstrap: int = 0
    auc: float | None = None
    message: str = ""
    model_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ModelInfo:
    trained_at: str
    n_positive: int
    n_negative: int
    n_bootstrap: int
    auc: float | None
    top_positive: list[tuple[str, float]] = field(default_factory=list)
    top_negative: list[tuple[str, float]] = field(default_factory=list)


def model_path(config: AppConfig) -> Path:
    """Store the model next to the SQLite DB (survives with the data it learned)."""

    url = config.database.url
    if url.startswith("sqlite:///"):
        db = Path(url.removeprefix("sqlite:///"))
        return db.parent / MODEL_FILENAME
    return Path("data") / MODEL_FILENAME


def featurize(feats: dict[str, Any]) -> dict[str, float]:
    """Feature dict -> flat {name: value} for DictVectorizer (tokens + numerics).

    A missing numeric gets a ``missing:`` flag instead of a value: "OMDb doesn't
    know it" is information, and after scaling an absent number sits at the mean.
    IMDb and TMDb votes stay separate features, their scales aren't comparable.
    """

    out: dict[str, float] = dict.fromkeys(tokens_from_features(feats), 1.0)

    def numeric(name: str, value: float | None) -> None:
        if value is None:
            out[f"missing:{name}"] = 1.0
        else:
            out[f"num:{name}"] = float(value)

    def log_votes(votes: Any) -> float | None:
        return None if votes is None else math.log10(votes + 10)

    popularity = feats.get("popularity")
    numeric("imdb_rating", feats.get("imdb_rating"))
    numeric("tmdb_rating", feats.get("tmdb_rating"))
    numeric("imdb_votes_log10", log_votes(feats.get("imdb_votes")))
    numeric("tmdb_votes_log10", log_votes(feats.get("tmdb_votes")))
    numeric("runtime_h", float(feats["runtime"]) / 60.0 if feats.get("runtime") else None)
    numeric("popularity", None if popularity is None else min(float(popularity) / 100.0, 10.0))
    if feats.get("kind") == "series":
        numeric("seasons", feats.get("seasons_count") or None)
    return out


def _fit_scales(rows: Iterable[dict[str, float]]) -> dict[str, tuple[float, float]]:
    """{numeric feature: (mean, std)}, learned from training rows only."""

    values: dict[str, list[float]] = {}
    for row in rows:
        for name, value in row.items():
            if name.startswith("num:"):
                values.setdefault(name, []).append(value)
    scales: dict[str, tuple[float, float]] = {}
    for name, seen in values.items():
        mean = sum(seen) / len(seen)
        std = math.sqrt(sum((v - mean) ** 2 for v in seen) / len(seen))
        scales[name] = (mean, std or 1.0)
    return scales


def _scaled(row: dict[str, float], scales: dict[str, tuple[float, float]]) -> dict[str, float]:
    out = dict(row)
    for name, (mean, std) in scales.items():
        if name in out:
            out[name] = (out[name] - mean) / std
    return out


def _is_rule_decision(c: Candidate) -> bool:
    """Approved by a rule rather than by you. Only ever true for a positive."""

    if c.status is CandidateStatus.rejected:
        return False
    if (c.features or {}).get("auto") or "(auto-grab)" in (c.reason or ""):
        return True
    return (
        c.source is CandidateSource.discovery
        and c.decided_at is not None
        and abs(c.decided_at - c.created_at) <= AUTO_DECISION_WINDOW
    )


def _training_rows(bootstrap: bool) -> tuple[list[_Row], int]:
    """One row per title (see the module docstring); returns (rows, n_bootstrap)."""

    rows: dict[int, _Row] = {}
    newest: dict[int, tuple[Any, int]] = {}
    n_bootstrap = 0
    with session_scope() as s:
        cands = s.scalars(
            select(Candidate).where(
                Candidate.features.is_not(None),
                Candidate.status.in_((*POSITIVE_STATUSES, CandidateStatus.rejected)),
            )
        ).all()
        for c in cands:
            # A quality upgrade is about a file, not the movie: rejecting one is no
            # dislike, approving one is no vote. Rejected rows must be skipped here,
            # because _is_rule_decision always counts a rejection.
            if c.source is CandidateSource.upgrade or _is_rule_decision(c):
                continue
            decided = (c.decided_at or c.created_at, c.id)
            if c.title_id not in newest or decided > newest[c.title_id]:
                newest[c.title_id] = decided
                label = 0 if c.status is CandidateStatus.rejected else 1
                rows[c.title_id] = _Row(featurize(c.features or {}), label, 1.0, c.title_id)

        if bootstrap:
            titles = s.scalars(
                select(Title)
                .options(selectinload(Title.genres))
                .where(
                    Title.tmdb_id.is_not(None),
                    exists().where(OwnedFile.title_id == Title.id) | Title.arr_has_file.is_(True),
                )
            ).all()
            watched = watched_title_ids(s)
            for t in titles:
                if t.id in rows:  # your decision outranks merely owning it
                    continue
                weight = WATCHED_BOOTSTRAP_WEIGHT if t.id in watched else BOOTSTRAP_WEIGHT
                rows[t.id] = _Row(featurize(extract_features(t)), 1, weight, t.id)
                n_bootstrap += 1
    return list(rows.values()), n_bootstrap


def _fit(rows: Sequence[_Row]) -> dict[str, Any]:
    """Everything prediction needs. The AUC folds and the deployed model both come
    from here, so the number reported describes the model that gets used."""

    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression

    scales = _fit_scales(r.x for r in rows)
    vec = DictVectorizer(sparse=True)
    x = vec.fit_transform([_scaled(r.x, scales) for r in rows])
    model = LogisticRegression(max_iter=2000, class_weight="balanced", C=1.0)
    model.fit(x, [r.y for r in rows], sample_weight=[r.weight for r in rows])
    return {"vectorizer": vec, "model": model, "scales": scales}


def _predict_rows(fitted: dict[str, Any], rows: Sequence[dict[str, float]]) -> list[float]:
    scaled = [_scaled(row, fitted["scales"]) for row in rows]
    x = fitted["vectorizer"].transform(scaled)
    return [float(p) for p in fitted["model"].predict_proba(x)[:, 1]]


def _cross_validate(rows: Sequence[_Row]) -> float | None:
    """AUC of the deployed recipe (bootstrap rows and weights included), scored on
    held-out real decisions, with every row of a title on one side of the split."""

    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedGroupKFold

    real = [r for r in rows if r.weight == 1.0]
    positives = sum(r.y for r in real)
    if len(real) < AUC_MIN_ROWS or min(positives, len(real) - positives) < AUC_MIN_PER_CLASS:
        return None

    folds = StratifiedGroupKFold(n_splits=AUC_FOLDS, shuffle=True, random_state=0)
    labels: list[int] = []
    scores: list[float] = []
    try:
        splits = folds.split(real, [r.y for r in real], groups=[r.title_id for r in real])
        for _, test_idx in splits:
            held_out = {real[i].title_id for i in test_idx}
            train_rows = [r for r in rows if r.title_id not in held_out]
            test_rows = [real[i] for i in test_idx]
            fitted = _fit(train_rows)
            scores.extend(_predict_rows(fitted, [r.x for r in test_rows]))
            labels.extend(r.y for r in test_rows)
        return round(float(roc_auc_score(labels, scores)), 3)
    except ValueError:  # a fold without both classes
        return None


def _save_bundle(bundle: dict[str, Any], path: Path) -> None:
    """Replace the model atomically: a crash mid-dump, or a reader mid-write, must
    never meet half a file. The temp file shares the directory so the rename is."""

    import joblib

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    try:
        joblib.dump(bundle, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def train(config: AppConfig, *, bootstrap: bool = True) -> TrainStats:
    """Fit + persist the preference model. Refuses politely on too few labels."""

    rows, n_bootstrap = _training_rows(bootstrap)
    n_pos = sum(1 for r in rows if r.y == 1 and r.weight == 1.0)
    n_neg = sum(1 for r in rows if r.y == 0)
    stats = TrainStats(n_positive=n_pos, n_negative=n_neg, n_bootstrap=n_bootstrap)

    if n_pos + n_neg < MIN_LABELS or min(n_pos, n_neg) < MIN_PER_CLASS:
        stats.message = (
            f"not enough decisions yet: {n_pos} approved-ish / {n_neg} rejected "
            f"(need ≥{MIN_LABELS} total, ≥{MIN_PER_CLASS} per class). "
            "Keep approving/rejecting candidates — every click is a label."
        )
        log.info("preferences.not_enough_labels", positive=n_pos, negative=n_neg)
        return stats

    stats.auc = _cross_validate(rows)
    bundle = _fit(rows)
    bundle["features_version"] = FEATURES_VERSION
    bundle["meta"] = {
        "trained_at": utcnow().isoformat(),
        "n_positive": n_pos,
        "n_negative": n_neg,
        "n_bootstrap": n_bootstrap,
        "auc": stats.auc,
    }
    path = model_path(config)
    _save_bundle(bundle, path)
    _cache.clear()
    stats.trained = True
    stats.model_path = str(path)
    stats.message = f"trained on {n_pos}+{n_neg} decisions (+{n_bootstrap} weak positives)"
    log.info("preferences.trained", **{k: v for k, v in stats.as_dict().items() if k != "message"})
    return stats


def _read_bundle(path: Path) -> dict[str, Any] | None:
    """The saved model if it can be used. Anything else is logged and means "no
    model": ranking without it beats failing discovery over a side input."""

    import joblib

    try:
        bundle = joblib.load(path)
    except Exception as exc:  # joblib/pickle raise all sorts on a damaged file
        log.warning("preferences.model_unreadable", path=str(path), error=redact_exc(exc))
        return None
    version = bundle.get("features_version") if isinstance(bundle, dict) else None
    if version != FEATURES_VERSION:
        log.warning("preferences.model_stale", found=version, expected=FEATURES_VERSION)
        return None
    loaded: dict[str, Any] = bundle
    return loaded


def _load(config: AppConfig) -> dict[str, Any] | None:
    path = model_path(config)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _cache.get(str(path))
    if cached and cached[0] == mtime:
        bundle: dict[str, Any] | None = cached[1]
        return bundle
    bundle = _read_bundle(path)
    _cache[str(path)] = (mtime, bundle)
    return bundle


def predict(config: AppConfig, feats: dict[str, Any]) -> float | None:
    """P(you'd approve) for a feature dict, or None when no usable model exists."""

    bundle = _load(config)
    if bundle is None:
        return None
    try:
        return round(_predict_rows(bundle, [featurize(feats)])[0], 3)
    except Exception as exc:
        log.warning("preferences.predict_failed", error=redact_exc(exc))
        return None


def _pretty_feature(name: str) -> str:
    if name.startswith("num:"):
        return name.removeprefix("num:")
    if name.startswith("missing:"):
        return f"no {name.removeprefix('missing:')}"
    return _pretty_token(name)


def model_info(config: AppConfig, top: int = 8) -> ModelInfo | None:
    """Metadata + the most influential tokens, for the insights page."""

    bundle = _load(config)
    if bundle is None:
        return None
    names = bundle["vectorizer"].get_feature_names_out()
    coefs = bundle["model"].coef_[0]
    order = coefs.argsort()
    pretty = [(_pretty_feature(n), round(float(c), 3)) for n, c in zip(names, coefs, strict=True)]
    meta = bundle["meta"]
    return ModelInfo(
        trained_at=meta["trained_at"],
        n_positive=meta["n_positive"],
        n_negative=meta["n_negative"],
        n_bootstrap=meta.get("n_bootstrap", 0),
        auc=meta.get("auc"),
        top_positive=[pretty[i] for i in order[::-1][:top] if pretty[i][1] > 0],
        top_negative=[pretty[i] for i in order[:top] if pretty[i][1] < 0],
    )


def require_model(config: AppConfig) -> None:
    if _load(config) is None:
        raise NotConfiguredError("No preference model trained yet (home-theater train).")
