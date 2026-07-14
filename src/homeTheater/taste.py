"""Content-based taste model over the owned library (plan §9 'personal insights').

Unsupervised ML that works from the catalog alone — no approve/reject labels:

* every title becomes a bag of namespaced tokens (genre, keyword, cast,
  director, language, decade, certification, collection) built from the same
  canonical feature dict as :mod:`homeTheater.features`;
* a per-kind TF-IDF space is fit over the owned library (keywords and cast are
  distinctive, ubiquitous genres get down-weighted automatically);
* **similarity**: a new title is scored by mean cosine similarity to its k
  nearest owned neighbors (0..1), with the neighbor titles returned so the
  dashboard can say *why* ("like: Blade Runner, Arrival");
* **clustering**: KMeans with silhouette-picked k characterizes the library
  ("your movie taste in 5 clusters"), each cluster labeled by its top terms
  and exemplar titles.

Everything is recomputed on demand — at home-library scale (hundreds to a few
thousand titles) fitting takes well under a second, so there is no model file
to persist or invalidate. scikit-learn imports are function-local to keep CLI
startup snappy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import selectinload

from .db.models import OwnedFile, Title, TitleKind
from .db.session import session_scope
from .features import extract_features
from .logging_setup import get_logger

log = get_logger(__name__)

# Token namespaces and their weight in the document (repetition = crude boost).
_TOKEN_REPEATS = {"g": 2, "kw": 1, "cast": 1, "dir": 2, "lang": 2, "dec": 1, "cert": 1, "col": 1}


def tokens_from_features(feats: dict[str, Any]) -> list[str]:
    """Namespaced token bag for one title's canonical feature dict."""

    tokens: list[str] = []

    def add(ns: str, value: Any) -> None:
        if value is None or value == "":
            return
        token = f"{ns}:{str(value).strip().lower()}"
        tokens.extend([token] * _TOKEN_REPEATS[ns])

    for g in feats.get("genres") or []:
        add("g", g)
    for k in feats.get("keywords") or []:
        add("kw", k)
    for c in (feats.get("cast_top") or [])[:10]:
        add("cast", c)
    for d in feats.get("directors") or []:
        add("dir", d)
    add("lang", feats.get("original_language"))
    add("dec", feats.get("decade"))
    add("cert", feats.get("certification"))
    if feats.get("in_collection"):
        add("col", feats.get("collection_name") or "yes")
    return tokens


@dataclass(frozen=True, slots=True)
class Similarity:
    score: float  # mean cosine similarity to the k nearest owned titles (0..1)
    like: list[str]  # those neighbors' titles, nearest first


@dataclass(frozen=True, slots=True)
class Cluster:
    label: str  # top distinguishing terms, human-readable
    size: int
    titles: list[str]  # exemplars, nearest-to-centroid first


class LibraryIndex:
    """TF-IDF index over the owned titles of one kind."""

    def __init__(self, kind: TitleKind, names: list[str], token_docs: list[list[str]]) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.kind = kind
        self.names = names
        self._vectorizer = TfidfVectorizer(analyzer=lambda doc: doc, min_df=1, norm="l2")
        self._matrix = self._vectorizer.fit_transform(token_docs)

    @property
    def size(self) -> int:
        return len(self.names)

    def similarity(self, feats: dict[str, Any], k: int = 5) -> Similarity:
        """Mean cosine similarity of ``feats`` to its k nearest owned titles."""

        import numpy as np

        vec = self._vectorizer.transform([tokens_from_features(feats)])
        sims = (self._matrix @ vec.T).toarray().ravel()  # unit-norm rows -> cosine
        k = min(k, sims.size)
        top = np.argsort(sims)[::-1][:k]
        score = float(np.mean(sims[top])) if k else 0.0
        like = [self.names[i] for i in top if sims[i] > 0]
        return Similarity(score=round(score, 3), like=like)

    def clusters(self, max_clusters: int = 8) -> list[Cluster]:
        """KMeans with silhouette-picked k; labels from top centroid terms."""

        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score

        n = self.size
        if n < 4:
            return [Cluster(label="library", size=n, titles=list(self.names))]

        upper = min(max_clusters, n - 1, max(2, n // 3))
        best_k, best_score, best_model = 2, -1.0, None
        for k in range(2, upper + 1):
            model = KMeans(n_clusters=k, n_init=10, random_state=0)
            labels = model.fit_predict(self._matrix)
            if len(set(labels)) < 2:
                continue
            score = silhouette_score(self._matrix, labels)
            if score > best_score:
                best_k, best_score, best_model = k, score, model
        if best_model is None:  # degenerate corpus (all identical docs)
            return [Cluster(label="library", size=n, titles=list(self.names))]

        terms = np.array(self._vectorizer.get_feature_names_out())
        labels = best_model.labels_
        out: list[Cluster] = []
        for c in range(best_k):
            members = np.flatnonzero(labels == c)
            if members.size == 0:
                continue
            centroid = best_model.cluster_centers_[c]
            top_idx = np.argsort(centroid)[::-1][:5]
            top_terms = [_pretty_token(t) for t in terms[top_idx] if centroid.max() > 0]
            dists = np.linalg.norm(self._matrix[members].toarray() - centroid, axis=1)
            exemplars = [self.names[members[i]] for i in np.argsort(dists)[:8]]
            out.append(
                Cluster(label=", ".join(top_terms[:4]), size=int(members.size), titles=exemplars)
            )
        out.sort(key=lambda c: c.size, reverse=True)
        return out


def _pretty_token(token: str) -> str:
    ns, _, value = token.partition(":")
    prefix = {
        "g": "",
        "kw": "",
        "cast": "with ",
        "dir": "by ",
        "lang": "in ",
        "dec": "",
        "cert": "rated ",
        "col": "franchise ",
    }.get(ns, "")
    if ns == "dec":
        return f"{value}s"
    return f"{prefix}{value}"


def build_index(kind: TitleKind, min_library: int = 8) -> LibraryIndex | None:
    """Fit a TF-IDF index over the owned+enriched titles of ``kind``.

    Returns None below ``min_library`` — a similarity score against three
    titles is noise, not signal.
    """

    with session_scope() as s:
        titles = s.scalars(
            select(Title)
            .options(selectinload(Title.genres))
            .where(
                Title.kind == kind,
                Title.tmdb_id.is_not(None),  # enriched only: features need metadata
                exists().where(OwnedFile.title_id == Title.id) | Title.arr_has_file.is_(True),
            )
        ).all()
        docs: list[tuple[str, list[str]]] = []
        for t in titles:
            tokens = tokens_from_features(extract_features(t))
            if tokens:
                docs.append((t.title, tokens))

    if len(docs) < min_library:
        log.info("taste.too_small", kind=kind.value, titles=len(docs), needed=min_library)
        return None
    names, token_docs = [d[0] for d in docs], [d[1] for d in docs]
    return LibraryIndex(kind, names, token_docs)
