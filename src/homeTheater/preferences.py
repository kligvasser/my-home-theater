"""Trained preference classifier (the plan's 'taste model, supervised half').

Training data = candidate feature snapshots (``Candidate.features``, frozen at
decision time) labeled by what you did with them:

* positive — approved / queued / downloading / imported
* negative — rejected

Optionally (``bootstrap``), owned titles are added as weak positives with a low
sample weight so the model has signal before many decisions exist. The model is
logistic regression over one-hot content tokens + numeric features — small,
fast, and *explainable*: coefficients say which tokens push toward approve.

The model rides ``data/preference_model.joblib`` next to the SQLite DB and is
blended into discovery scoring only when it exists (``taste.model_weight``).
kNN similarity (homeTheater.taste) answers "is it like what I own?"; this model
answers the sharper question "is it like what I *approve*?".
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import selectinload

from .config import AppConfig
from .db.base import utcnow
from .db.models import Candidate, CandidateStatus, OwnedFile, Title
from .db.session import session_scope
from .errors import NotConfiguredError
from .features import extract_features
from .logging_setup import get_logger
from .taste import _pretty_token, tokens_from_features

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

_cache: dict[str, Any] = {}  # path -> (mtime, bundle)


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
    """Feature dict -> flat {name: value} for DictVectorizer (tokens + numerics)."""

    out: dict[str, float] = dict.fromkeys(tokens_from_features(feats), 1.0)
    if feats.get("imdb_rating") is not None:
        out["num:imdb_rating"] = float(feats["imdb_rating"])
    votes = feats.get("imdb_votes") or feats.get("tmdb_votes")
    if votes:
        out["num:votes_log10"] = math.log10(votes + 10)
    if feats.get("runtime"):
        out["num:runtime_h"] = float(feats["runtime"]) / 60.0
    if feats.get("popularity") is not None:
        out["num:popularity"] = min(float(feats["popularity"]) / 100.0, 10.0)
    if feats.get("seasons_count"):
        out["num:seasons"] = float(feats["seasons_count"])
    return out


def _training_rows(bootstrap: bool) -> tuple[list[dict[str, float]], list[int], list[float], int]:
    xs: list[dict[str, float]] = []
    ys: list[int] = []
    weights: list[float] = []
    n_bootstrap = 0
    with session_scope() as s:
        cands = s.scalars(
            select(Candidate).where(
                Candidate.features.is_not(None),
                Candidate.status.in_((*POSITIVE_STATUSES, CandidateStatus.rejected)),
            )
        ).all()
        for c in cands:
            xs.append(featurize(c.features or {}))
            ys.append(0 if c.status is CandidateStatus.rejected else 1)
            weights.append(1.0)

        if bootstrap:
            titles = s.scalars(
                select(Title)
                .options(selectinload(Title.genres))
                .where(
                    Title.tmdb_id.is_not(None),
                    exists().where(OwnedFile.title_id == Title.id) | Title.arr_has_file.is_(True),
                )
            ).all()
            for t in titles:
                xs.append(featurize(extract_features(t)))
                ys.append(1)
                weights.append(BOOTSTRAP_WEIGHT)
                n_bootstrap += 1
    return xs, ys, weights, n_bootstrap


def train(config: AppConfig, *, bootstrap: bool = True) -> TrainStats:
    """Fit + persist the preference model. Refuses politely on too few labels."""

    import joblib
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import cross_val_predict

    xs, ys, weights, n_bootstrap = _training_rows(bootstrap)
    n_pos = sum(1 for y, w in zip(ys, weights, strict=True) if y == 1 and w == 1.0)
    n_neg = ys.count(0)
    stats = TrainStats(n_positive=n_pos, n_negative=n_neg, n_bootstrap=n_bootstrap)

    if n_pos + n_neg < MIN_LABELS or min(n_pos, n_neg) < MIN_PER_CLASS:
        stats.message = (
            f"not enough decisions yet: {n_pos} approved-ish / {n_neg} rejected "
            f"(need ≥{MIN_LABELS} total, ≥{MIN_PER_CLASS} per class). "
            "Keep approving/rejecting candidates — every click is a label."
        )
        log.info("preferences.not_enough_labels", positive=n_pos, negative=n_neg)
        return stats

    vec = DictVectorizer(sparse=True)
    x = vec.fit_transform(xs)
    model = LogisticRegression(max_iter=2000, class_weight="balanced", C=1.0)

    # Honest-ish quality number on the *real* labels only (bootstrap excluded).
    real_idx = [i for i, w in enumerate(weights) if w == 1.0]
    if len(real_idx) >= 30 and min(n_pos, n_neg) >= 10:
        try:
            proba = cross_val_predict(
                model, x[real_idx], [ys[i] for i in real_idx], cv=3, method="predict_proba"
            )[:, 1]
            stats.auc = round(float(roc_auc_score([ys[i] for i in real_idx], proba)), 3)
        except ValueError:  # a fold without both classes
            stats.auc = None

    model.fit(x, ys, sample_weight=weights)
    path = model_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "vectorizer": vec,
            "model": model,
            "meta": {
                "trained_at": utcnow().isoformat(),
                "n_positive": n_pos,
                "n_negative": n_neg,
                "n_bootstrap": n_bootstrap,
                "auc": stats.auc,
            },
        },
        path,
    )
    _cache.clear()
    stats.trained = True
    stats.model_path = str(path)
    stats.message = f"trained on {n_pos}+{n_neg} decisions (+{n_bootstrap} weak positives)"
    log.info("preferences.trained", **{k: v for k, v in stats.as_dict().items() if k != "message"})
    return stats


def _load(config: AppConfig) -> dict[str, Any] | None:
    path = model_path(config)
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _cache.get(str(path))
    if cached and cached[0] == mtime:
        bundle: dict[str, Any] = cached[1]
        return bundle
    import joblib

    bundle = joblib.load(path)
    _cache[str(path)] = (mtime, bundle)
    return bundle


def predict(config: AppConfig, feats: dict[str, Any]) -> float | None:
    """P(you'd approve) for a feature dict, or None when no model exists."""

    bundle = _load(config)
    if bundle is None:
        return None
    x = bundle["vectorizer"].transform([featurize(feats)])
    return round(float(bundle["model"].predict_proba(x)[0, 1]), 3)


def model_info(config: AppConfig, top: int = 8) -> ModelInfo | None:
    """Metadata + the most influential tokens, for the insights page."""

    bundle = _load(config)
    if bundle is None:
        return None
    names = bundle["vectorizer"].get_feature_names_out()
    coefs = bundle["model"].coef_[0]
    order = coefs.argsort()
    pretty = [
        (n.removeprefix("num:") if n.startswith("num:") else _pretty_token(n), round(float(c), 3))
        for n, c in zip(names, coefs, strict=True)
    ]
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
