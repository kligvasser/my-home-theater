"""Kodi metadata on the NAS: ``.nfo`` files and artwork beside what we own.

Kodi can build its library from local files alone. With a source's scraper set to
"Local information only" it never calls a metadata server, so a player that can't
reach TMDb still shows the full library. It reads:

* a movie: ``<video>.nfo``, ``<video>-poster.jpg``, ``<video>-fanart.jpg``;
* a show: ``tvshow.nfo``, ``poster.jpg`` and ``fanart.jpg`` in the show's folder, and
  ``<video>.nfo`` beside each episode. Without that last file Kodi has nowhere to
  learn an episode from and leaves it out. Everything written here comes from the
catalog and the TMDb responses already cached for it; only the images are fetched.

Rules:

* An ``.nfo`` somebody else wrote is left alone. Ours carries :data:`MARKER`; a
  release group's text file by the same name is not Kodi's and is replaced.
* A write happens only when the content differs, and artwork is fetched once, so a
  run with nothing new does no NAS writes and no downloads.
* An episode's file says only which episode it is ("Episode 4" of season 2): the
  catalog holds no per-episode titles or plots, and Kodi needs none to list and play.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any
from xml.sax.saxutils import escape

import httpx
from sqlalchemy import select

from .config import AppConfig
from .db.models import MetadataCache, OwnedFile, Title, TitleKind
from .db.session import session_scope
from .errors import redact_exc
from .logging_setup import get_logger
from .nas_paths import MountPathError, unc_to_mount

log = get_logger(__name__)

MARKER = "<!-- written by my-home-theater -->"
IMAGE_HOST = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w780"
FANART_SIZE = "w1280"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ACTORS = 15
_IMAGE_TIMEOUT_S = 30.0
_NFO_READ_BYTES = 256 * 1024
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


@dataclass
class ExportStats:
    movies: int = 0
    shows: int = 0
    episodes: int = 0
    nfo_written: int = 0
    nfo_unchanged: int = 0
    nfo_foreign: int = 0  # somebody else's Kodi .nfo, left alone
    artwork_written: int = 0
    missing: int = 0  # in the catalog, not on the NAS
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Movie:
    path: str  # catalog (UNC) path of the video
    title: str
    year: int | None
    tmdb_id: int
    imdb_id: str | None
    runtime: int | None
    overview: str | None
    release_date: str | None
    certification: str | None
    imdb_rating: float | None
    imdb_votes: int | None
    tmdb_rating: float | None
    tmdb_votes: int | None
    genres: tuple[str, ...]
    directors: tuple[str, ...]
    cast: tuple[tuple[str, str], ...]  # (name, role); role may be empty
    countries: tuple[str, ...]
    collection: str | None
    original_title: str | None
    tagline: str | None
    poster_path: str | None  # TMDb image path, e.g. "/abc.jpg"
    backdrop_path: str | None


def _tag(name: str, value: object, indent: str = "  ") -> str:
    return f"{indent}<{name}>{escape(str(value))}</{name}>\n"


def _rating(name: str, value: float | None, votes: int | None, *, default: bool) -> str:
    if value is None:
        return ""
    flag = ' default="true"' if default else ""
    return (
        f'    <rating name="{name}" max="10"{flag}>\n'
        f"      <value>{value:.1f}</value>\n"
        f"      <votes>{votes or 0}</votes>\n"
        "    </rating>\n"
    )


def movie_nfo(m: _Movie) -> bytes:
    """The Kodi movie ``.nfo`` for ``m``. Deterministic: same input, same bytes."""

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n', MARKER + "\n", "<movie>\n"]
    out.append(_tag("title", m.title))
    if m.original_title and m.original_title != m.title:
        out.append(_tag("originaltitle", m.original_title))
    if m.year:
        out.append(_tag("year", m.year))
    if m.release_date:
        out.append(_tag("premiered", m.release_date))
    if m.runtime:
        out.append(_tag("runtime", m.runtime))
    if m.tagline:
        out.append(_tag("tagline", m.tagline))
    if m.overview:
        out.append(_tag("plot", m.overview))
    if m.certification:
        out.append(_tag("mpaa", m.certification))
    out.append(f'  <uniqueid type="tmdb" default="true">{m.tmdb_id}</uniqueid>\n')
    if m.imdb_id:
        out.append(f'  <uniqueid type="imdb">{escape(m.imdb_id)}</uniqueid>\n')
    ratings = _rating("imdb", m.imdb_rating, m.imdb_votes, default=True) + _rating(
        "themoviedb", m.tmdb_rating, m.tmdb_votes, default=m.imdb_rating is None
    )
    if ratings:
        out.append(f"  <ratings>\n{ratings}  </ratings>\n")
    out.extend(_tag("genre", g) for g in m.genres)
    out.extend(_tag("country", c) for c in m.countries)
    out.extend(_tag("director", d) for d in m.directors)
    if m.collection:
        out.append(f"  <set>\n{_tag('name', m.collection, '    ')}  </set>\n")
    for order, (name, role) in enumerate(m.cast):
        out.append("  <actor>\n" + _tag("name", name, "    "))
        if role:
            out.append(_tag("role", role, "    "))
        out.append(_tag("order", order, "    ") + "  </actor>\n")
    out.append("</movie>\n")
    return "".join(out).encode("utf-8")


def _cached_details(session: Any, tmdb_id: int, kind: str = "movie") -> dict[str, Any]:
    """The newest cached TMDb details for a title, however old: this reads what we
    already hold and must not turn an export into a round of API calls."""

    row = session.scalars(
        select(MetadataCache)
        .where(
            MetadataCache.provider == "tmdb",
            MetadataCache.cache_key.like(f"details:%:{kind}:{tmdb_id}:%"),
        )
        .order_by(MetadataCache.fetched_at.desc())
        .limit(1)
    ).first()
    payload = row.payload if row is not None else None
    return payload if isinstance(payload, dict) else {}


def _image_path(value: object) -> str | None:
    """A TMDb image path we are willing to put in a URL and a filename."""

    if not isinstance(value, str) or not value.startswith("/") or ".." in value:
        return None
    if "/" in value[1:] or not value.lower().endswith(_IMAGE_EXTENSIONS):
        return None
    return value


def _cast(details: dict[str, Any], fallback: list[str]) -> tuple[tuple[str, str], ...]:
    credits = details.get("credits")
    cast = credits.get("cast") if isinstance(credits, dict) else None
    people: list[tuple[str, str]] = []
    if isinstance(cast, list):
        for person in cast:
            if isinstance(person, dict) and person.get("name"):
                people.append((str(person["name"]), str(person.get("character") or "")))
    if not people:
        people = [(name, "") for name in fallback]
    return tuple(people[:MAX_ACTORS])


def _collect() -> list[_Movie]:
    movies: list[_Movie] = []
    with session_scope() as s:
        rows = s.execute(
            select(OwnedFile, Title)
            .join(Title, OwnedFile.title_id == Title.id)
            .where(OwnedFile.kind == TitleKind.movie, Title.tmdb_id.is_not(None))
            .order_by(OwnedFile.path)
        ).all()
        for owned, title in rows:
            details = _cached_details(s, title.tmdb_id)
            poster = _image_path(details.get("poster_path"))
            if poster is None and title.poster_url:
                poster = _image_path("/" + title.poster_url.rsplit("/", 1)[-1])
            movies.append(
                _Movie(
                    path=owned.path,
                    title=title.title,
                    year=title.year,
                    tmdb_id=title.tmdb_id,
                    imdb_id=title.imdb_id,
                    runtime=title.runtime,
                    overview=title.overview,
                    release_date=title.release_date,
                    certification=title.certification,
                    imdb_rating=title.imdb_rating,
                    imdb_votes=title.imdb_votes,
                    tmdb_rating=title.tmdb_rating,
                    tmdb_votes=title.tmdb_votes,
                    genres=tuple(sorted(g.name for g in title.genres)),
                    directors=tuple(title.directors or ()),
                    cast=_cast(details, list(title.cast_top or ())),
                    countries=tuple(title.origin_countries or ()),
                    collection=title.collection_name,
                    original_title=str(details.get("original_title") or "") or None,
                    tagline=str(details.get("tagline") or "") or None,
                    poster_path=poster,
                    backdrop_path=_image_path(details.get("backdrop_path")),
                )
            )
    return movies


def _is_foreign_nfo(path: str, root_tag: str = "<movie") -> bool:
    """True for a Kodi ``.nfo`` we didn't write. Unreadable counts as foreign."""

    try:
        with open(path, "rb") as fh:
            head = fh.read(_NFO_READ_BYTES).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return root_tag in head.lower() and MARKER not in head


def _same_content(path: str, data: bytes) -> bool:
    try:
        if os.path.getsize(path) != len(data):
            return False
        with open(path, "rb") as fh:
            return fh.read() == data
    except OSError:
        return False


def _fetch_image(client: httpx.Client, size: str, image_path: str) -> bytes:
    with client.stream("GET", f"{IMAGE_HOST}/{size}{image_path}") as resp:
        resp.raise_for_status()
        if not resp.headers.get("content-type", "").startswith("image/"):
            raise ValueError("not an image")
        body = bytearray()
        for chunk in resp.iter_bytes():
            body += chunk
            if len(body) > MAX_IMAGE_BYTES:
                raise ValueError("image is too large")
    if not body:
        raise ValueError("empty image")
    return bytes(body)


def _export_one(m: _Movie, base: str, client: httpx.Client | None, stats: ExportStats) -> None:
    video = unc_to_mount(m.path, base)
    if not os.path.isfile(video):
        stats.missing += 1
        return
    stats.movies += 1
    stem_path = os.path.splitext(video)[0]
    _write_nfo(stem_path + ".nfo", movie_nfo(m), "<movie", stats)
    _write_artwork(
        (
            (f"{stem_path}-poster", POSTER_SIZE, m.poster_path),
            (f"{stem_path}-fanart", FANART_SIZE, m.backdrop_path),
        ),
        m.title,
        client,
        stats,
    )


def _write_nfo(path: str, data: bytes, root_tag: str, stats: ExportStats) -> None:
    from .subtitles.native.placement import write_subtitle as write_atomic

    if _same_content(path, data):
        stats.nfo_unchanged += 1
    elif _is_foreign_nfo(path, root_tag):
        stats.nfo_foreign += 1
    else:
        write_atomic(path, data)
        stats.nfo_written += 1


def _write_artwork(
    targets: tuple[tuple[str, str, str | None], ...],
    title: str,
    client: httpx.Client | None,
    stats: ExportStats,
) -> None:
    """``targets``: (path without extension, TMDb size, TMDb image path)."""

    from .subtitles.native.placement import write_subtitle as write_atomic

    if client is None:
        return
    for dest, size, image_path in targets:
        if image_path is None or any(os.path.exists(dest + e) for e in _IMAGE_EXTENSIONS):
            continue
        try:
            image = _fetch_image(client, size, image_path)
        except (httpx.HTTPError, ValueError) as exc:
            stats.errors.append(f"{title}: {os.path.basename(dest)}: {redact_exc(exc)}")
            continue
        write_atomic(dest + os.path.splitext(image_path)[1].lower(), image)
        stats.artwork_written += 1


@dataclass(frozen=True, slots=True)
class _Episode:
    path: str  # catalog (UNC) path of the video
    season: int
    first: int
    last: int  # equals ``first`` unless the file holds several episodes


@dataclass(frozen=True, slots=True)
class _Show:
    title: str
    year: int | None
    tmdb_id: int
    imdb_id: str | None
    tvdb_id: int | None
    overview: str | None
    release_date: str | None
    certification: str | None
    status: str | None
    imdb_rating: float | None
    imdb_votes: int | None
    tmdb_rating: float | None
    tmdb_votes: int | None
    genres: tuple[str, ...]
    cast: tuple[tuple[str, str], ...]
    countries: tuple[str, ...]
    original_title: str | None
    poster_path: str | None
    backdrop_path: str | None
    episodes: tuple[_Episode, ...]


def tvshow_nfo(show: _Show) -> bytes:
    """The Kodi ``tvshow.nfo`` for ``show``. Deterministic."""

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n', MARKER + "\n", "<tvshow>\n"]
    out.append(_tag("title", show.title))
    if show.original_title and show.original_title != show.title:
        out.append(_tag("originaltitle", show.original_title))
    if show.year:
        out.append(_tag("year", show.year))
    if show.release_date:
        out.append(_tag("premiered", show.release_date))
    if show.overview:
        out.append(_tag("plot", show.overview))
    if show.certification:
        out.append(_tag("mpaa", show.certification))
    if show.status:
        out.append(_tag("status", show.status))
    out.append(f'  <uniqueid type="tmdb" default="true">{show.tmdb_id}</uniqueid>\n')
    if show.imdb_id:
        out.append(f'  <uniqueid type="imdb">{escape(show.imdb_id)}</uniqueid>\n')
    if show.tvdb_id:
        out.append(f'  <uniqueid type="tvdb">{show.tvdb_id}</uniqueid>\n')
    ratings = _rating("imdb", show.imdb_rating, show.imdb_votes, default=True) + _rating(
        "themoviedb", show.tmdb_rating, show.tmdb_votes, default=show.imdb_rating is None
    )
    if ratings:
        out.append(f"  <ratings>\n{ratings}  </ratings>\n")
    out.extend(_tag("genre", g) for g in show.genres)
    out.extend(_tag("country", c) for c in show.countries)
    for order, (name, role) in enumerate(show.cast):
        out.append("  <actor>\n" + _tag("name", name, "    "))
        if role:
            out.append(_tag("role", role, "    "))
        out.append(_tag("order", order, "    ") + "  </actor>\n")
    out.append("</tvshow>\n")
    return "".join(out).encode("utf-8")


def episode_nfo(show_title: str, ep: _Episode) -> bytes:
    """The ``.nfo`` for one episode file: one block per episode it holds, which is
    how Kodi reads a double episode."""

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n', MARKER + "\n"]
    for number in range(ep.first, ep.last + 1):
        out.append(
            "<episodedetails>\n"
            + _tag("title", f"Episode {number}")
            + _tag("showtitle", show_title)
            + _tag("season", ep.season)
            + _tag("episode", number)
            + "</episodedetails>\n"
        )
    return "".join(out).encode("utf-8")


_MAX_EPISODES_PER_FILE = 20  # a wider range is a misparse, not a real file


def _collect_shows() -> list[_Show]:
    shows: list[_Show] = []
    with session_scope() as s:
        titles = s.scalars(
            select(Title)
            .where(Title.kind == TitleKind.series, Title.tmdb_id.is_not(None))
            .order_by(Title.title)
        ).all()
        for title in titles:
            tmdb_id = title.tmdb_id
            if tmdb_id is None:
                continue
            episodes: list[_Episode] = []
            for owned in sorted(title.owned_files, key=lambda f: f.path):
                if owned.kind != TitleKind.series or owned.season is None or owned.episode is None:
                    continue
                last = owned.episode_end if owned.episode_end is not None else owned.episode
                if not 0 <= last - owned.episode < _MAX_EPISODES_PER_FILE:
                    last = owned.episode
                episodes.append(_Episode(owned.path, owned.season, owned.episode, last))
            if not episodes:
                continue
            details = _cached_details(s, tmdb_id, "series")
            poster = _image_path(details.get("poster_path"))
            if poster is None and title.poster_url:
                poster = _image_path("/" + title.poster_url.rsplit("/", 1)[-1])
            shows.append(
                _Show(
                    title=title.title,
                    year=title.year,
                    tmdb_id=tmdb_id,
                    imdb_id=title.imdb_id,
                    tvdb_id=title.tvdb_id,
                    overview=title.overview,
                    release_date=title.release_date,
                    certification=title.certification,
                    status=title.series_status,
                    imdb_rating=title.imdb_rating,
                    imdb_votes=title.imdb_votes,
                    tmdb_rating=title.tmdb_rating,
                    tmdb_votes=title.tmdb_votes,
                    genres=tuple(sorted(g.name for g in title.genres)),
                    cast=_cast(details, list(title.cast_top or ())),
                    countries=tuple(title.origin_countries or ()),
                    original_title=str(details.get("original_name") or "") or None,
                    poster_path=poster,
                    backdrop_path=_image_path(details.get("backdrop_path")),
                    episodes=tuple(episodes),
                )
            )
    return shows


def _show_folder(video: str, tv_root: str) -> str | None:
    """The show's own folder: the first one below the TV root. None for a file that
    sits in the root itself or outside it."""

    rel = os.path.relpath(video, tv_root)
    parts = rel.split(os.sep)
    if len(parts) < 2 or parts[0] in ("", ".", ".."):
        return None
    return os.path.join(tv_root, parts[0])


def _export_show(
    show: _Show,
    base: str,
    tv_root: str,
    claimed: set[str],
    client: httpx.Client | None,
    stats: ExportStats,
    cancel: threading.Event | None,
) -> None:
    folders: list[str] = []
    for ep in show.episodes:
        if cancel is not None and cancel.is_set():
            return
        try:
            video = unc_to_mount(ep.path, base)
        except MountPathError as exc:
            stats.errors.append(f"{show.title}: {redact_exc(exc)}")
            continue
        if not os.path.isfile(video):
            stats.missing += 1
            continue
        stats.episodes += 1
        _write_nfo(
            os.path.splitext(video)[0] + ".nfo",
            episode_nfo(show.title, ep),
            "<episodedetails",
            stats,
        )
        folder = _show_folder(video, tv_root)
        if folder is not None and folder not in folders:
            folders.append(folder)

    for folder in folders:
        # Two catalog titles sharing one folder can't both describe it: first wins,
        # and the order (by title) is the same on every run.
        if folder in claimed:
            continue
        claimed.add(folder)
        stats.shows += 1
        _write_nfo(os.path.join(folder, "tvshow.nfo"), tvshow_nfo(show), "<tvshow", stats)
        _write_artwork(
            (
                (os.path.join(folder, "poster"), POSTER_SIZE, show.poster_path),
                (os.path.join(folder, "fanart"), FANART_SIZE, show.backdrop_path),
            ),
            show.title,
            client,
            stats,
        )


def export_library(config: AppConfig, cancel: threading.Event | None = None) -> ExportStats:
    """Write what's missing or out of date for every owned movie and show. Blocking:
    NAS I/O and image downloads, so call it from a worker thread. Needs the mount."""

    from .acquisition.torrent.importer import ensure_mounted

    stats = ExportStats()
    base = config.torrent.library_base_dir
    if not base:
        stats.errors.append("torrent.library_base_dir (the mount) is not set")
        return stats
    try:
        ensure_mounted(base, None)
    except Exception as exc:
        stats.errors.append(redact_exc(exc))
        return stats

    movies = _collect()
    shows = _collect_shows()
    tv_root = os.path.join(base, *config.nas.tv_root.split("/"))
    claimed: set[str] = set()
    client = (
        httpx.Client(timeout=_IMAGE_TIMEOUT_S, follow_redirects=True)
        if config.nfo.artwork
        else None
    )
    try:
        for m in movies:
            if cancel is not None and cancel.is_set():
                break
            try:
                _export_one(m, base, client, stats)
            except (OSError, MountPathError) as exc:
                stats.errors.append(f"{m.title}: {redact_exc(exc)}")
                log.warning("nfo.export_failed", title=m.title, detail=redact_exc(exc))
        for show in shows:
            if cancel is not None and cancel.is_set():
                break
            try:
                _export_show(show, base, tv_root, claimed, client, stats, cancel)
            except (OSError, MountPathError) as exc:
                stats.errors.append(f"{show.title}: {redact_exc(exc)}")
                log.warning("nfo.export_failed", title=show.title, detail=redact_exc(exc))
    finally:
        if client is not None:
            client.close()
    log.info("nfo.exported", **{k: v for k, v in stats.as_dict().items() if k != "errors"})
    return stats


_EXPORT_TIMEOUT_SECONDS = 1800.0


async def run_export(config: AppConfig, cancel: threading.Event | None = None) -> ExportStats:
    """:func:`export_library` under the ``sync`` lock: the WD MyCloud throws EIO when
    NAS writes overlap an import. Raises JobBusyError if one is running."""

    from .cleanup import _on_nas_thread
    from .locks import job_lock

    cancel = cancel or threading.Event()
    with job_lock(config, "sync"):
        try:
            return await _on_nas_thread(_EXPORT_TIMEOUT_SECONDS, export_library, config, cancel)
        except TimeoutError:
            cancel.set()
            raise
