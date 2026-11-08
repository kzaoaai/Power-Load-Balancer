"""
Balancing engine module for the Power Load Balancer integration.

This module contains the logic for balancing power consumption by turning
appliances on and off based on the configured power budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback

from .const import (
    CONF_APPLIANCE,
    CONF_IMPORTANCE,
    CONF_LAST_RESORT,
    NON_BINARY_ACTIVE_STATE_DOMAINS,
    RESTORE_MARGIN_FRACTION,
    RESTORE_UNKNOWN_SIZE_FRACTION,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class BalancingCallbacks:
    """Callbacks used by the balancing engine."""

    get_total_power: Callable[[], float]
    get_expected_power_restoration: Callable[[str], float]
    get_sensor_power_for_appliance: Callable[[str], float]
    cancel_scheduled_turn_on: Callable[[str], None]
    reduce_estimated_power: Callable[[float], None]
    is_appliance_balanced_off: Callable[[str], bool]
    is_in_cooldown: Callable[[str], bool]
    is_appliance_shed: Callable[[str], bool]
    get_settling_power: Callable[[], float]
    restore_spacing_remaining: Callable[[], float]


class BalancingEngine:
    """
    Manages the power balancing logic.

    This class determines when and which appliances should be turned off
    to stay within the power budget, and when appliances can be safely
    restored.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        monitored_sensors: list[dict[str, Any]],
        power_budget: int,
    ) -> None:
        """
        Initialize the BalancingEngine.

        Args:
            hass: Home Assistant instance.
            monitored_sensors: List of monitored sensor configurations.
            power_budget: Maximum power budget in watts.

        """
        self.hass = hass
        self._monitored_sensors = monitored_sensors
        self._power_budget = power_budget
        self._effective_budget: float = float(power_budget)
        self._reported_balance_down_failure = False

    def set_power_budget(self, power_budget: int) -> None:
        """Update the power budget at runtime."""
        self._power_budget = power_budget

    def set_effective_budget(self, effective_budget: float) -> None:
        """
        Update the effective budget used for restoration headroom.

        Restorations are only allowed when the appliance fits under the
        effective budget, so the balancer never turns an appliance back on
        into a load level that would immediately trigger shedding again.
        """
        self._effective_budget = effective_budget

    def _is_non_binary_active_state_entity(self, entity_id: str) -> bool:
        """Check if an entity uses non-binary active states."""
        return entity_id.startswith(
            tuple(f"{domain}." for domain in NON_BINARY_ACTIVE_STATE_DOMAINS)
        )

    def _is_appliance_active(self, entity_id: str, state: str) -> bool:
        """Check if an appliance is in an active state."""
        if self._is_non_binary_active_state_entity(entity_id):
            return state not in ("off", "unknown", "unavailable")
        return state == "on"

    @callback
    def _importance_of(self, appliance_entity_id: str) -> float:
        """Return an appliance's configured importance; higher is kept longer."""
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == appliance_entity_id:
                try:
                    return float(sensor_config.get(CONF_IMPORTANCE, 5))
                except (TypeError, ValueError):
                    return 5.0
        return 5.0

    def balance_up(
        self,
        callbacks: BalancingCallbacks,
        balanced_off_appliances: list[str],
        restore_appliance_callback: Any,
    ) -> None:
        """
        Turn on appliances that can safely fit within power budget.

        Args:
            callbacks: Callbacks for power and appliance operations.
            balanced_off_appliances: List of appliances turned off by the balancer.
            restore_appliance_callback: Callback to restore an appliance.

        """
        # Most important first: the ladder sheds the least important first,
        # so restoring in shed order brought the least important back first
        # whenever there was room for only one.
        appliances_to_restore = sorted(
            balanced_off_appliances,
            key=self._importance_of,
            reverse=True,
        )

        _LOGGER.debug(
            "Considering appliances for restoration: %s", appliances_to_restore
        )

        if appliances_to_restore and callbacks.restore_spacing_remaining() > 0:
            _LOGGER.debug("Restores spaced out: the last one is still settling")
            return
        settling_power = callbacks.get_settling_power()

        for appliance_entity_id in appliances_to_restore:
            if callbacks.is_in_cooldown(appliance_entity_id):
                continue

            appliance_state = self.hass.states.get(appliance_entity_id)
            if (
                appliance_state
                and appliance_state.state != "off"
                and not callbacks.is_appliance_shed(appliance_entity_id)
            ):
                continue

            current_power = callbacks.get_total_power() + settling_power
            available_budget = self._effective_budget - current_power

            expected_power = callbacks.get_expected_power_restoration(
                appliance_entity_id
            )
            if expected_power <= 0:
                expected_power = callbacks.get_sensor_power_for_appliance(
                    appliance_entity_id
                )

            _LOGGER.debug(
                "Restoration check for %s: Current power %s W, Available budget %s W, "
                "Expected power %s W",
                appliance_entity_id,
                current_power,
                available_budget,
                expected_power,
            )

            unknown_size = expected_power <= 0
            if unknown_size:
                # Nothing to size the restore against, so wait for a house
                # quiet enough that almost anything fits.
                fits = current_power < self._effective_budget * (
                    RESTORE_UNKNOWN_SIZE_FRACTION
                )
                if fits:
                    _LOGGER.debug(
                        "Restoring %s on headroom alone: its draw is unknown "
                        "and the load is %s W, below %s%% of the effective "
                        "budget",
                        appliance_entity_id,
                        current_power,
                        int(RESTORE_UNKNOWN_SIZE_FRACTION * 100),
                    )
            else:
                margin = self._effective_budget * RESTORE_MARGIN_FRACTION
                fits = available_budget - margin >= expected_power

            if fits:
                _LOGGER.info(
                    "Restoring %s: sufficient power headroom "
                    "(%s W available, %s W needed)",
                    appliance_entity_id,
                    available_budget,
                    expected_power,
                )

                callbacks.cancel_scheduled_turn_on(appliance_entity_id)

                async def restore_task(
                    appliance_entity_id: str = appliance_entity_id,
                    expected_power: float = expected_power,
                ) -> None:
                    try:
                        await restore_appliance_callback(
                            appliance_entity_id,
                            "restored by automatic balancing",
                            expected_power,
                        )
                    except Exception:
                        _LOGGER.exception(
                            "Failed to restore appliance %s", appliance_entity_id
                        )

                self.hass.async_create_task(restore_task())
                return

        _LOGGER.debug("No appliances eligible for restoration")

    def shed_candidates(
        self,
        is_appliance_shed_callback: Any,
        is_appliance_balanced_off_callback: Any,
    ) -> Iterator[dict[str, Any]]:
        """
        Yield the appliances that could be shed, least important first.

        Shared with the pending-action report so what the balancer says it is
        about to do and what it would actually do cannot drift apart.
        """
        sensors_to_consider = sorted(
            self._monitored_sensors, key=lambda x: x.get(CONF_IMPORTANCE, 5)
        )
        for sensor_config in sensors_to_consider:
            appliance_entity_id = sensor_config.get(CONF_APPLIANCE)
            if not appliance_entity_id:
                continue
            if sensor_config.get(CONF_LAST_RESORT, False):
                continue
            appliance_state = self.hass.states.get(appliance_entity_id)
            if appliance_state is None:
                continue
            if is_appliance_shed_callback(appliance_entity_id):
                continue
            if not self._is_appliance_active(
                appliance_entity_id, appliance_state.state
            ):
                continue
            if is_appliance_balanced_off_callback(appliance_entity_id):
                continue
            yield sensor_config

    def next_shed_candidate(
        self,
        is_appliance_shed_callback: Any,
        is_appliance_balanced_off_callback: Any,
    ) -> str | None:
        """Return the appliance that would be shed next, if any."""
        for sensor_config in self.shed_candidates(
            is_appliance_shed_callback, is_appliance_balanced_off_callback
        ):
            return str(sensor_config[CONF_APPLIANCE])
        return None

    @callback
    def balance_down(  # noqa: PLR0913  (one callback per collaborator, by design)
        self,
        is_appliance_shed_callback: Any,
        get_sensor_power_for_appliance_callback: Any,
        reduce_estimated_power_callback: Any,
        is_appliance_balanced_off_callback: Any,
        turn_off_appliance_callback: Any,
        reason: str | None = None,
    ) -> bool:
        """
        Turn off appliances to bring power usage below budget.

        Args:
            is_appliance_shed_callback: Callback reporting whether an
                appliance already reports itself shed.
            get_sensor_power_for_appliance_callback: Callback to get sensor
                power for appliance.
            reduce_estimated_power_callback: Callback to reduce estimated power.
            is_appliance_balanced_off_callback: Callback to check if appliance
                is balanced off.
            turn_off_appliance_callback: Callback to turn off an appliance.
            reason: Optional reason recorded with the shed. Defaults to the
                over-budget message.

        Returns:
            True if a turn-off was initiated, False if no appliance was
            eligible for shedding.

        """
        shed_reason = reason or (
            f"Automatic balancing: Exceeded budget of {self._power_budget} W"
        )

        for sensor_config in self.shed_candidates(
            is_appliance_shed_callback, is_appliance_balanced_off_callback
        ):
            appliance_entity_id = sensor_config[CONF_APPLIANCE]
            expected_power_reduction = get_sensor_power_for_appliance_callback(
                appliance_entity_id
            )

            _LOGGER.info(
                "Turning off %s (importance %s) to balance power. "
                "Expected power reduction: %s W",
                appliance_entity_id,
                sensor_config.get(CONF_IMPORTANCE),
                expected_power_reduction,
            )

            optimistic_reduction = 0.0
            if expected_power_reduction > 0:
                optimistic_reduction = expected_power_reduction
                reduce_estimated_power_callback(optimistic_reduction)
                _LOGGER.debug(
                    "Optimistically reduced estimated power by %s W for %s",
                    optimistic_reduction,
                    appliance_entity_id,
                )

            async def turn_off_task(
                inner_appliance_id: str, reduced_power: float
            ) -> None:
                try:
                    appliance_state_before = self.hass.states.get(inner_appliance_id)
                    if appliance_state_before and not self._is_appliance_active(
                        inner_appliance_id, appliance_state_before.state
                    ):
                        _LOGGER.debug(
                            "Skipping turn_off: appliance %s already in %s",
                            inner_appliance_id,
                            appliance_state_before.state,
                        )
                        return

                    await turn_off_appliance_callback(
                        inner_appliance_id,
                        shed_reason,
                    )
                except Exception:
                    if reduced_power > 0:
                        reduce_estimated_power_callback(-reduced_power)
                        _LOGGER.debug(
                            "Restored estimated power by %s W for %s after "
                            "turn-off failure",
                            reduced_power,
                            inner_appliance_id,
                        )
                    _LOGGER.exception(
                        "Failed to turn off appliance %s", inner_appliance_id
                    )

            self.hass.async_create_task(
                turn_off_task(appliance_entity_id, optimistic_reduction)
            )
            self._reported_balance_down_failure = False
            return True

        if self._reported_balance_down_failure:
            _LOGGER.debug(
                "Still unable to balance power using non-last-resort appliances"
            )
        else:
            _LOGGER.warning(
                "Could not balance power below budget by turning off non-last-resort "
                "appliances."
            )
            self._reported_balance_down_failure = True
        return False
