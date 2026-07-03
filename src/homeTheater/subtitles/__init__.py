"""Subtitle coverage + Bazarr-triggered search (plan §5.5)."""

from .bazarr import BazarrClient, WantedItem
from .service import SweepStats, sweep_missing

__all__ = ["BazarrClient", "SweepStats", "WantedItem", "sweep_missing"]
