"""Discovery: candidate sources, threshold filtering/scoring, and the engine."""

from .filters import FilterOutcome, evaluate, score
from .service import DiscoveryStats, run_discovery
from .sources import Discovered, build_sources

__all__ = [
    "Discovered",
    "DiscoveryStats",
    "FilterOutcome",
    "build_sources",
    "evaluate",
    "run_discovery",
    "score",
]
