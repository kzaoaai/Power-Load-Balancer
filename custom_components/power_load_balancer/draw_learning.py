"""
What each appliance draws while it runs, learned from its own sensor.

A shed appliance reads zero, and a sensor that polls once a minute reads zero
for a while after an appliance starts, so neither can size what letting it run
would cost. A learned figure answers that the way a nameplate would, without
the user having to know the nameplate.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from .const import (
    CONF_NOMINAL_POWER_WATT,
    CONF_POWER_DRAW_MODE,
    LEARN_MIN_SAMPLES,
    LEARN_PERCENTILE,
    LEARN_RUNNING_FLOOR_WATT,
    LEARN_WINDOW_SAMPLES,
    POWER_DRAW_FIXED,
    POWER_DRAW_LEARN,
    POWER_DRAW_MODES,
)


def resolve_power_draw_mode(sensor_config: dict[str, Any]) -> str:
    """
    Return whether an appliance's draw is learned or fixed.

    An appliance configured before the choice existed has no mode: it is fixed
    when it carries a nameplate value, learned when it does not, so an upgrade
    changes nothing for it.
    """
    mode = sensor_config.get(CONF_POWER_DRAW_MODE)
    if mode in POWER_DRAW_MODES:
        return str(mode)
    try:
        nominal = float(sensor_config.get(CONF_NOMINAL_POWER_WATT) or 0)
    except (TypeError, ValueError):
        nominal = 0.0
    return POWER_DRAW_FIXED if nominal > 0 else POWER_DRAW_LEARN


class DrawLearner:
    """Keep recent running readings per appliance and report a high percentile."""

    def __init__(self) -> None:
        """Start with nothing learned."""
        self._samples: dict[str, deque[int]] = {}

    def add(self, entity_id: str, watts: float) -> bool:
        """
        Record a reading taken while the appliance may be running.

        Returns whether it was kept: standby readings and anything that is not
        a finite number are not.
        """
        if not math.isfinite(watts) or watts < LEARN_RUNNING_FLOOR_WATT:
            return False
        samples = self._samples.setdefault(
            entity_id, deque(maxlen=LEARN_WINDOW_SAMPLES)
        )
        samples.append(round(watts))
        return True

    def learned(self, entity_id: str) -> float:
        """Return the learned draw in watts, or 0 while too little is known."""
        samples = self._samples.get(entity_id)
        if not samples or len(samples) < LEARN_MIN_SAMPLES:
            return 0.0
        ordered = sorted(samples)
        rank = math.ceil(LEARN_PERCENTILE / 100 * len(ordered))
        return float(ordered[max(rank, 1) - 1])

    def sample_count(self, entity_id: str) -> int:
        """Return how many running readings are held for an appliance."""
        return len(self._samples.get(entity_id, ()))

    def forget_except(self, entity_ids: set[str]) -> None:
        """Drop what was learned for appliances no longer configured."""
        for entity_id in list(self._samples):
            if entity_id not in entity_ids:
                del self._samples[entity_id]

    def snapshot(self) -> dict[str, Any]:
        """Return the readings in a form the store can save."""
        return {
            "samples": {
                entity_id: list(samples) for entity_id, samples in self._samples.items()
            }
        }

    def load(self, saved: Any) -> None:
        """Take back readings saved before a restart, ignoring anything malformed."""
        samples = saved.get("samples") if isinstance(saved, dict) else None
        if not isinstance(samples, dict):
            return
        for entity_id, values in samples.items():
            if not isinstance(entity_id, str) or not isinstance(values, list):
                continue
            for value in values[-LEARN_WINDOW_SAMPLES:]:
                if isinstance(value, int | float) and not isinstance(value, bool):
                    self.add(entity_id, float(value))

    def diagnostics(self) -> dict[str, Any]:
        """Return what has been learned, for troubleshooting."""
        return {
            entity_id: {
                "samples": len(samples),
                "learned_watt": self.learned(entity_id),
            }
            for entity_id, samples in self._samples.items()
        }
