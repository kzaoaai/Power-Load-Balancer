"""
Monotonic time source for the Power Load Balancer.

Dwell times, cooldowns and shed ages are all durations, so they must come
from a clock that cannot jump: a wall-clock correction in the middle of a
sustained-load episode would otherwise shed an appliance that had been at
level for seconds. Routing every duration through one function also gives
tests a single seam to control, instead of patching the event loop's own
clock and breaking asyncio's scheduling along with it.
"""

from __future__ import annotations

import time


def monotonic() -> float:
    """Return a monotonic timestamp in seconds."""
    return time.monotonic()
