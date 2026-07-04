"""Native subtitle backend (subtitles.backend == 'native').

Search subtitle providers directly (OpenSubtitles, ktuvit) and write the ``.srt``
next to each owned media file, driven by our own catalog coverage — no Bazarr /
Radarr / Sonarr. See :mod:`homeTheater.subtitles.native.service`.
"""

from .base import SubtitleQuery, SubtitleResult, SubtitleSource, opensubtitles_hash
from .service import sweep_native

__all__ = [
    "SubtitleQuery",
    "SubtitleResult",
    "SubtitleSource",
    "opensubtitles_hash",
    "sweep_native",
]
