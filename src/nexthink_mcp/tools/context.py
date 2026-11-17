from __future__ import annotations

from dataclasses import dataclass

from ..client import NexthinkClient
from ..config import Settings

# Appended to every tool description so an agent never mistakes this for a live view.
LATENCY = (
    "Data latency: Nexthink data is near-real-time but typically some minutes behind (assumed, not verified); "
    "the newest ~15 minutes of events may be missing. All times are UTC."
)


@dataclass
class AppContext:
    settings: Settings
    client: NexthinkClient


def bounded(name: str, value: int, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer between {lo} and {hi}")
    return value
