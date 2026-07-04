"""Subtitle coverage + fetching (plan §5.5): Bazarr or native providers."""

from .bazarr import BazarrClient, WantedItem
from .service import SweepStats, sweep_missing, sweep_subtitles

__all__ = ["BazarrClient", "SweepStats", "WantedItem", "sweep_missing", "sweep_subtitles"]
