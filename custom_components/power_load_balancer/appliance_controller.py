"""
Appliance control module for the Power Load Balancer integration.

This module handles turning appliances on and off, scheduling automatic turn-ons,
and managing appliance state tracking.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_ENTITY_ID
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .clock import monotonic
from .const import (
    CONF_APPLIANCE,
    CONF_DEVICE_COOLDOWN,
    CONF_NOMINAL_POWER_WATT,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_NOMINAL_POWER_WATT,
    DOMAIN,
    ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE,
    NON_BINARY_ACTIVE_STATE_DOMAINS,
    NOTIFY_PROBLEM,
    NOTIFY_RESTORE,
    NOTIFY_SHED,
    OWN_RESTORE_GRACE_SECONDS,
    POWER_DRAW_FIXED,
    POWER_DRAW_LEARN,
    RESTORE_MARGIN_FRACTION,
    RESTORE_SETTLE_SECONDS,
    RESTORE_SETTLED_FRACTION,
    RESTORE_SPACING_SECONDS,
    SHED_AWARE_PLATFORMS,
    SHED_STORE_SAVE_DELAY_SECONDS,
    appliance_control_issue_id,
)
from .context_logger import ContextLogger
from .draw_learning import DrawLearner, resolve_power_draw_mode
from .exceptions import (
    ApplianceControlError,
    EntityNotFoundError,
    EntityUnavailableError,
    RetryableError,
    ServiceCallError,
    ServiceTimeoutError,
)
from .retry import retry_with_backoff
from .service import ServiceCallParams, safe_service_call
from .validation import validate_entity_id, validate_entity_state

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import Context, HomeAssistant, State
    from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)


class ApplianceController:
    """
    Manages appliance control operations and scheduling.

    This class handles turning appliances on and off, tracking which appliances
    have been turned off by the balancer, and scheduling automatic restoration
    after a configured delay.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        monitored_sensors: list[dict[str, Any]],
        global_cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        event_log_sensor: Any | None = None,
    ) -> None:
        """
        Initialize the ApplianceController.

        Args:
            hass: Home Assistant instance.
            monitored_sensors: List of monitored sensor configurations.
            global_cooldown_seconds: Global cooldown time in seconds before
                auto turn-on.
            event_log_sensor: Optional event log sensor for logging events.

        """
        self.hass = hass
        self._monitored_sensors = monitored_sensors
        self._global_cooldown_seconds = global_cooldown_seconds
        self._event_log_sensor = event_log_sensor
        self._balanced_off_appliances: dict[str, Any] = {}
        self._scheduled_auto_turn_ons: dict[str, asyncio.Task[Any]] = {}
        self._expected_power_restoration: dict[str, float] = {}
        self._previous_hvac_modes: dict[str, str] = {}
        self._previous_operation_modes: dict[str, str] = {}
        self._own_restores: dict[str, float] = {}
        self._settling: dict[str, dict[str, float]] = {}
        self.learner = DrawLearner()
        self.power_of: Callable[[str], float] = lambda _entity_id: 0.0
        self._last_restore_at: float | None = None
        self._store: Store[dict[str, Any]] | None = None

    def attach_store(self, store: Store[dict[str, Any]]) -> None:
        """Persist the shed bookkeeping to this store from now on."""
        self._store = store

    def _shed_snapshot(self) -> dict[str, Any]:
        """Return what must survive a restart for every appliance held off."""
        return {
            "appliances": {
                entity_id: {
                    "reason": info.get("reason"),
                    "hvac_mode": self._previous_hvac_modes.get(entity_id),
                    "operation_mode": self._previous_operation_modes.get(entity_id),
                    "expected_power": self._expected_power_restoration.get(entity_id),
                    "shed_at": info.get("shed_at"),
                }
                for entity_id, info in self._balanced_off_appliances.items()
            }
        }

    def _schedule_save(self) -> None:
        """
        Queue a write of the shed bookkeeping.

        The snapshot is taken now, not when the write happens, so clearing the
        in-memory state on unload can never be written over the record.
        """
        if self._store is None:
            return
        snapshot = self._shed_snapshot()
        self._store.async_delay_save(lambda: snapshot, SHED_STORE_SAVE_DELAY_SECONDS)

    async def async_flush_store(self) -> None:
        """Write the shed bookkeeping now, ahead of an unload."""
        if self._store is not None:
            await self._store.async_save(self._shed_snapshot())

    def adopt_saved_record(
        self, entity_id: str, record: dict[str, Any], *, mark: bool = True
    ) -> None:
        """
        Take back an appliance recorded as shed before a restart or reload.

        The cooldown starts again from now: how long the appliance was off
        before is unknown here, and waiting once more is the safe side.
        """
        if mark:
            reason = record.get("reason") or "Turned off by Power Load Balancer"
            self.mark_appliance_balanced_off(
                entity_id, f"adopted from saved record: {reason}"
            )
            shed_at = record.get("shed_at")
            if isinstance(shed_at, str) and shed_at:
                self._balanced_off_appliances[entity_id]["shed_at"] = shed_at
        hvac_mode = record.get("hvac_mode")
        if isinstance(hvac_mode, str) and hvac_mode:
            self._previous_hvac_modes[entity_id] = hvac_mode
        operation_mode = record.get("operation_mode")
        if isinstance(operation_mode, str) and operation_mode:
            self._previous_operation_modes[entity_id] = operation_mode
        expected_power = record.get("expected_power")
        if isinstance(expected_power, int | float) and expected_power > 0:
            self._expected_power_restoration[entity_id] = float(expected_power)
        self._schedule_save()

    def set_event_log_sensor(self, sensor: Any) -> None:
        """
        Set the event log sensor instance.

        Args:
            sensor: The PowerBalancerLogSensor instance.

        """
        self._event_log_sensor = sensor

    def get_sensor_for_appliance(self, appliance_entity_id: str) -> str | None:
        """
        Get the monitored sensor entity ID associated with an appliance.

        Args:
            appliance_entity_id: Entity ID of the appliance.

        Returns:
            Entity ID of the associated sensor, or None if not found.

        """
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == appliance_entity_id:
                return sensor_config.get(CONF_ENTITY_ID)
        return None

    def get_appliance_for_sensor(self, sensor_entity_id: str) -> str | None:
        """
        Get the appliance entity ID associated with a monitored sensor.

        Args:
            sensor_entity_id: Entity ID of the sensor.

        Returns:
            Entity ID of the associated appliance, or None if not found.

        """
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_ENTITY_ID) == sensor_entity_id:
                return sensor_config.get(CONF_APPLIANCE)
        return None

    def is_appliance_balanced_off(self, entity_id: str) -> bool:
        """
        Check if an appliance was turned off by the balancer.

        Args:
            entity_id: Entity ID of the appliance.

        Returns:
            True if the appliance was turned off by the balancer, False otherwise.

        """
        return entity_id in self._balanced_off_appliances

    def mark_appliance_balanced_off(self, entity_id: str, reason: str) -> None:
        """
        Mark an appliance as turned off by the balancer.

        Args:
            entity_id: Entity ID of the appliance.
            reason: Reason for turning off the appliance.

        """
        self._balanced_off_appliances[entity_id] = {
            "timestamp": monotonic(),
            "reason": reason or "Turned off by Power Load Balancer",
            "shed_at": dt_util.utcnow().replace(microsecond=0).isoformat(),
        }
        self._settling.pop(entity_id, None)
        self._schedule_save()

    def get_shed_info(self, entity_id: str) -> dict[str, Any] | None:
        """Return why and when an appliance was shed, if it is held off."""
        return self._balanced_off_appliances.get(entity_id)

    def note_restore_settling(self, entity_id: str, expected_power: float) -> None:
        """
        Record a restore that has landed but may not be drawing yet.

        Its expected draw is held back from the headroom until it shows up on
        its own sensor or the hold runs out, and the next restore waits out
        the spacing.
        """
        now = monotonic()
        self._last_restore_at = now
        self._settling[entity_id] = {
            "watts": max(float(expected_power), 0.0),
            "since": now,
        }

    def settling_power(self, power_of: Callable[[str], float]) -> float:
        """
        Return the draw still expected from restores not yet seen drawing.

        A hold ends when the appliance's sensor reads at least half of what it
        was expected to draw (its draw is in the meter now), or after
        RESTORE_SETTLE_SECONDS, whichever comes first.
        """
        now = monotonic()
        total = 0.0
        for entity_id, hold in list(self._settling.items()):
            watts = hold["watts"]
            expired = now - hold["since"] >= RESTORE_SETTLE_SECONDS
            drawing = watts > 0 and power_of(entity_id) >= (
                watts * RESTORE_SETTLED_FRACTION
            )
            if expired or drawing:
                self._settling.pop(entity_id, None)
                continue
            total += watts
        return total

    def restore_spacing_remaining(self) -> float:
        """Return how long until the next restore may start, in seconds."""
        if self._last_restore_at is None:
            return 0.0
        return max(0.0, RESTORE_SPACING_SECONDS - (monotonic() - self._last_restore_at))

    def last_restored(self) -> str | None:
        """Return the appliance restored most recently, while it settles."""
        if not self._settling:
            return None
        return max(self._settling, key=lambda e: self._settling[e]["since"])

    def expire_cooldowns_since(self, since: float) -> list[str]:
        """
        End the cooldown of every appliance shed at or after a moment.

        Returns the appliances released from their cooldown.
        """
        released = []
        for entity_id, info in self._balanced_off_appliances.items():
            if info.get("timestamp", 0) >= since:
                info["timestamp"] = (
                    monotonic() - self.get_cooldown_for_appliance(entity_id) - 1
                )
                released.append(entity_id)
        return released

    def restart_cooldown(self, entity_id: str) -> None:
        """Hold an appliance off for another cooldown before the next attempt."""
        info = self._balanced_off_appliances.get(entity_id)
        if info is not None:
            info["timestamp"] = monotonic()

    def get_balanced_off_age(self, entity_id: str) -> float | None:
        """Return seconds since an appliance was marked balanced off."""
        info = self._balanced_off_appliances.get(entity_id)
        if info is None:
            return None
        return monotonic() - info.get("timestamp", 0)

    def remove_from_balanced_off(self, entity_id: str) -> None:
        """
        Remove an appliance from the balanced off tracking.

        Args:
            entity_id: Entity ID of the appliance.

        """
        if self._balanced_off_appliances.pop(entity_id, None) is not None:
            self._schedule_save()

    def get_balanced_off_appliances(self) -> list[str]:
        """
        Get list of appliances that were turned off by the balancer.

        Returns:
            List of appliance entity IDs.

        """
        return list(self._balanced_off_appliances.keys())

    def cancel_scheduled_turn_on(self, entity_id: str) -> None:
        """
        Cancel a scheduled automatic turn-on for an appliance.

        Args:
            entity_id: Entity ID of the appliance.

        """
        if entity_id in self._scheduled_auto_turn_ons:
            task = self._scheduled_auto_turn_ons[entity_id]
            if not task.done():
                task.cancel()
            self._scheduled_auto_turn_ons.pop(entity_id, None)
            self._expected_power_restoration.pop(entity_id, None)

    def set_expected_power_restoration(
        self, entity_id: str, expected_power: float
    ) -> None:
        """
        Set the expected power consumption when an appliance is restored.

        Args:
            entity_id: Entity ID of the appliance.
            expected_power: Expected power consumption in watts.

        """
        self._expected_power_restoration[entity_id] = expected_power

    def get_expected_power_restoration(self, entity_id: str) -> float:
        """
        Get the expected power consumption when an appliance is restored.

        Args:
            entity_id: Entity ID of the appliance.

        Returns:
            Expected power consumption in watts, or 0.0 if not set.

        """
        return self._expected_power_restoration.get(entity_id, 0.0)

    def _is_climate_entity(self, entity_id: str) -> bool:
        """
        Check if an entity is a climate entity.

        Args:
            entity_id: Entity ID to check.

        Returns:
            True if the entity is a climate entity, False otherwise.

        """
        return entity_id.startswith("climate.")

    def _get_shed_api(self, entity_id: str) -> dict[str, str] | None:
        """
        Return the managed-shed service contract for an entity, if any.

        Some integrations expose dedicated shed/release services that suppress
        an appliance without looking like a person operating it. Entity-level
        calls such as set_operation_mode are indistinguishable from manual
        control and can trip an integration's own manual-override handling, so
        the dedicated services are preferred whenever both the entity's
        platform and the running services support them.
        """
        platform = self._get_entity_platform(entity_id)
        if platform is None:
            return None

        api = SHED_AWARE_PLATFORMS.get(platform)
        if api is None:
            return None

        if not self.hass.services.has_service(api["domain"], api["shed"]):
            return None

        return api

    def is_appliance_shed(self, entity_id: str) -> bool:
        """
        Return True when an appliance reports itself shed by the balancer.

        An integration with a managed shed service suppresses the load without
        changing the operation mode the user chose, so the entity keeps
        reporting an active-looking state while drawing nothing. Treating that
        state as "still running" would stop the balancer from ever releasing
        it, so the platform's shed attribute is authoritative here.
        """
        return self.state_reports_shed(entity_id, self.hass.states.get(entity_id))

    def state_reports_shed(self, entity_id: str, state: State | None) -> bool:
        """
        Return True when a given state snapshot reports the appliance shed.

        Taking the snapshot rather than reading the entity lets callers judge
        the state an appliance is moving away from as well as the one it is
        moving to, which is the only way to notice a shed being cleared: the
        reported mode is identical on both sides of that change.
        """
        if state is None:
            return False

        api = SHED_AWARE_PLATFORMS.get(self._get_entity_platform(entity_id) or "")
        attribute = (api or {}).get("shed_attribute")
        if not attribute:
            return False

        return bool(state.attributes.get(attribute))

    def has_stopped(self, entity_id: str) -> bool:
        """
        Return True when an appliance reports that it is no longer running.

        A turn-off whose service call failed or timed out can still have taken
        effect. A plain appliance shows that by reading "off"; one suppressed
        through a managed shed keeps reporting its operation mode and flags the
        shed in an attribute instead, so both have to count. Reading only
        "off" forgot every shed tank whose shed call errored after it landed,
        and nothing ever released it.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        return state.state == "off" or self.state_reports_shed(entity_id, state)

    def note_own_restore(self, entity_id: str) -> None:
        """
        Record that the balancer itself is about to restore an appliance.

        The appliance's return to drawing reaches the turn-on handler exactly
        like a person switching it on. Without this record the handler's
        budget veto judged the balancer's own restore as an override and shed
        the appliance again within the same second.
        """
        self._own_restores[entity_id] = monotonic()

    def forget_own_restore(self, entity_id: str) -> None:
        """Drop the record of a restore that did not go through."""
        self._own_restores.pop(entity_id, None)

    def consume_own_restore(self, entity_id: str) -> bool:
        """
        Return True, once, when the balancer restored this appliance just now.

        The record is used up by the first turn-on it explains, so a later
        turn-on by a person is still judged by the veto.
        """
        started = self._own_restores.pop(entity_id, None)
        if started is None:
            return False
        return monotonic() - started <= OWN_RESTORE_GRACE_SECONDS

    def get_nominal_power(self, entity_id: str) -> float:
        """
        Return an appliance's configured nameplate draw, or 0 if none is set.

        A shed appliance draws nothing, and a power sensor that polls once a
        minute reads nothing for a while after one starts, so neither can size
        what letting it run would cost. A nameplate answers immediately. Zero
        means unknown, never that the appliance is free.
        """
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) != entity_id:
                continue
            try:
                return max(
                    float(
                        sensor_config.get(
                            CONF_NOMINAL_POWER_WATT, DEFAULT_NOMINAL_POWER_WATT
                        )
                        or 0
                    ),
                    0.0,
                )
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def get_power_draw_mode(self, entity_id: str) -> str:
        """Return whether an appliance's draw is learned or fixed."""
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == entity_id:
                return resolve_power_draw_mode(sensor_config)
        return POWER_DRAW_LEARN

    def get_reference_power(self, entity_id: str) -> float:
        """
        Return what an appliance draws when its sensor cannot say, or 0.

        The fixed value in fixed mode, the learned one in learn mode. Zero
        means unknown, never that the appliance is free.
        """
        if self.get_power_draw_mode(entity_id) == POWER_DRAW_FIXED:
            return self.get_nominal_power(entity_id)
        return self.learner.learned(entity_id)

    def _get_entity_platform(self, entity_id: str) -> str | None:
        """Return the integration platform that provides an entity."""
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry.platform if entry else None

    def _is_water_heater_entity(self, entity_id: str) -> bool:
        """
        Check if an entity is a water_heater entity.

        Args:
            entity_id: Entity ID to check.

        Returns:
            True if the entity is a water_heater entity, False otherwise.

        """
        return entity_id.startswith("water_heater.")

    def _is_non_binary_active_state_entity(self, entity_id: str) -> bool:
        """Check if an entity uses non-binary active states."""
        return entity_id.startswith(
            tuple(f"{domain}." for domain in NON_BINARY_ACTIVE_STATE_DOMAINS)
        )

    def _get_supported_hvac_modes(self, entity_id: str) -> list[str]:
        """Get supported HVAC modes for a climate entity."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return []

        hvac_modes = state.attributes.get("hvac_modes", [])
        if not isinstance(hvac_modes, list):
            return []

        return [str(mode) for mode in hvac_modes]

    def _get_current_hvac_mode(self, appliance_state: State) -> str | None:
        """Get current HVAC mode from state attributes with safe fallback."""
        hvac_mode = appliance_state.attributes.get("hvac_mode")
        if isinstance(hvac_mode, str) and hvac_mode:
            return hvac_mode

        if appliance_state.state not in ("unknown", "unavailable"):
            return appliance_state.state

        return None

    def _prepare_climate_turn_off(
        self, entity_id: str, appliance_state: State, logger: ContextLogger, reason: str
    ) -> tuple[str, dict[str, str]]:
        """
        Prepare climate entity turn-off with HVAC mode saving.

        Saves the current HVAC mode before turning off the climate entity.

        Args:
            entity_id: Entity ID of the climate entity.
            appliance_state: Current state of the entity.
            logger: Context logger for this operation.
            reason: Reason for turning off the appliance.

        Returns:
            Tuple of (service, service_data)

        """
        current_hvac_mode = self._get_current_hvac_mode(appliance_state)
        supported_hvac_modes = self._get_supported_hvac_modes(entity_id)

        if current_hvac_mode is not None and self._is_valid_active_hvac_mode(
            current_hvac_mode, supported_hvac_modes
        ):
            self.set_previous_hvac_mode(entity_id, str(current_hvac_mode))
        else:
            logger.warning(
                "Climate is active but current HVAC mode cannot be saved",
                entity_id=entity_id,
                current_hvac_mode=current_hvac_mode,
                supported_hvac_modes=supported_hvac_modes,
            )

        logger.debug(
            "Turning off climate entity",
            entity_id=entity_id,
            previous_mode=self.get_previous_hvac_mode(entity_id),
            reason=reason,
        )

        return (
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": "off"},
        )

    def _prepare_water_heater_turn_off(
        self, entity_id: str, appliance_state: State, logger: ContextLogger, reason: str
    ) -> tuple[str, dict[str, str]]:
        """
        Prepare water_heater entity turn-off with operation mode saving.

        Saves the current operation mode before turning off the water_heater.

        Args:
            entity_id: Entity ID of the water_heater entity.
            appliance_state: Current state of the entity.
            logger: Context logger for this operation.
            reason: Reason for turning off the appliance.

        Returns:
            Tuple of (service, service_data)

        """
        current_mode = appliance_state.state
        if current_mode not in ("off", "unknown", "unavailable"):
            self._previous_operation_modes[entity_id] = current_mode
            self._schedule_save()
            logger.debug(
                "Saved water_heater operation mode",
                entity_id=entity_id,
                operation_mode=current_mode,
            )
        else:
            logger.warning(
                "Water heater is active but current mode cannot be saved",
                entity_id=entity_id,
                current_mode=current_mode,
            )

        operation_list = appliance_state.attributes.get("operation_list")
        if isinstance(operation_list, list) and "off" not in operation_list:
            logger.debug(
                "Water heater does not support the 'off' operation mode, "
                "falling back to turn_off",
                entity_id=entity_id,
                operation_list=operation_list,
            )
            return ("turn_off", {"entity_id": entity_id})

        logger.debug(
            "Turning off water_heater entity",
            entity_id=entity_id,
            previous_mode=self._previous_operation_modes.get(entity_id),
            reason=reason,
        )

        return (
            "set_operation_mode",
            {"entity_id": entity_id, "operation_mode": "off"},
        )

    def _is_valid_active_hvac_mode(
        self, mode: str | None, supported_modes: list[str]
    ) -> bool:
        """Check if a mode is valid and represents an active HVAC mode."""
        if not mode:
            return False

        if mode in ("off", "unknown", "unavailable"):
            return False

        return not supported_modes or mode in supported_modes

    def _select_fallback_hvac_mode(self, entity_id: str) -> str | None:
        """Select a fallback active HVAC mode for restoration."""
        for mode in self._get_supported_hvac_modes(entity_id):
            if mode not in ("off", "unknown", "unavailable"):
                return mode
        return None

    def _resolve_hvac_restore_mode(
        self, entity_id: str, appliance_state: State
    ) -> str | None:
        """Resolve the best HVAC mode to restore when turning climate back on."""
        supported_modes = self._get_supported_hvac_modes(entity_id)

        previous_mode = self.get_previous_hvac_mode(entity_id)
        if self._is_valid_active_hvac_mode(previous_mode, supported_modes):
            return previous_mode

        current_mode = self._get_current_hvac_mode(appliance_state)
        if self._is_valid_active_hvac_mode(current_mode, supported_modes):
            return current_mode

        return self._select_fallback_hvac_mode(entity_id)

    def _prepare_turn_on_service_call(
        self,
        entity_id: str,
        appliance_state: State,
        reason: str,
        logger: ContextLogger,
    ) -> tuple[str, str, dict[str, str]] | None:
        """Prepare domain/service payload for turning on an appliance."""
        domain = entity_id.split(".", maxsplit=1)[0]

        shed_api = self._get_shed_api(entity_id)
        if shed_api is not None and (
            self.state_reports_shed(entity_id, appliance_state)
            or entity_id not in self._previous_operation_modes
        ):
            # Restore the way the appliance was shed. A saved operation mode
            # means it was turned off through set_operation_mode, typically
            # because the managed shed service was not registered yet at
            # startup; releasing it would leave it in "off" for good.
            logger.debug(
                "Releasing appliance through its managed release service",
                entity_id=entity_id,
                service=f"{shed_api['domain']}.{shed_api['release']}",
                reason=reason,
            )
            return (
                shed_api["domain"],
                shed_api["release"],
                {"entity_id": entity_id},
            )

        if self._is_climate_entity(entity_id):
            previous_mode = self._resolve_hvac_restore_mode(entity_id, appliance_state)
            if previous_mode is None:
                supported_hvac_modes = self._get_supported_hvac_modes(entity_id)
                logger.warning(
                    "Skipping climate restore: no valid HVAC mode available",
                    entity_id=entity_id,
                    stored_previous_mode=self.get_previous_hvac_mode(entity_id),
                    supported_hvac_modes=supported_hvac_modes,
                )
                if self._event_log_sensor:
                    self._event_log_sensor.add_log_entry(
                        f"Service call skipped: Cannot restore {entity_id}. "
                        "No valid HVAC mode available",
                        category=NOTIFY_PROBLEM,
                    )
                return None

            logger.debug(
                "Restoring climate entity to previous mode",
                entity_id=entity_id,
                hvac_mode=previous_mode,
                reason=reason,
            )
            return (
                domain,
                "set_hvac_mode",
                {
                    "entity_id": entity_id,
                    "hvac_mode": previous_mode,
                },
            )

        if self._is_water_heater_entity(entity_id):
            previous_mode = self._previous_operation_modes.get(entity_id)
            if previous_mode and previous_mode not in ("off", "unknown", "unavailable"):
                logger.debug(
                    "Restoring water_heater to previous operation mode",
                    entity_id=entity_id,
                    operation_mode=previous_mode,
                    reason=reason,
                )
                return (
                    domain,
                    "set_operation_mode",
                    {
                        "entity_id": entity_id,
                        "operation_mode": previous_mode,
                    },
                )

            # Fallback: use turn_on
            logger.debug(
                "No previous operation mode for water_heater, using turn_on",
                entity_id=entity_id,
                reason=reason,
            )
            return domain, "turn_on", {"entity_id": entity_id}

        logger.debug(
            "Calling turn_on service",
            entity_id=entity_id,
            domain=domain,
            reason=reason,
        )
        return domain, "turn_on", {"entity_id": entity_id}

    def _is_appliance_active(self, entity_id: str, state: str) -> bool:
        """
        Check if an appliance is in an active state.

        For climate entities, any state other than 'off' is considered active.
        For other entities, only 'on' is considered active.

        Args:
            entity_id: Entity ID of the appliance.
            state: Current state of the appliance.

        Returns:
            True if the appliance is active, False otherwise.

        """
        if self.is_appliance_shed(entity_id):
            return False
        if self._is_non_binary_active_state_entity(entity_id):
            return state not in ("off", "unknown", "unavailable")
        return state == "on"

    def _is_appliance_off(self, _entity_id: str, state: str) -> bool:
        """
        Check if an appliance is in an off state.

        Args:
            _entity_id: Entity ID of the appliance (unused, for API consistency).
            state: Current state of the appliance.

        Returns:
            True if the appliance is off, False otherwise.

        """
        return state == "off" or self.is_appliance_shed(_entity_id)

    def get_previous_hvac_mode(self, entity_id: str) -> str | None:
        """
        Get the previous HVAC mode for a climate entity.

        Args:
            entity_id: Entity ID of the climate entity.

        Returns:
            Previous HVAC mode, or None if not stored.

        """
        return self._previous_hvac_modes.get(entity_id)

    def set_previous_hvac_mode(self, entity_id: str, hvac_mode: str) -> None:
        """
        Store the previous HVAC mode for a climate entity.

        Args:
            entity_id: Entity ID of the climate entity.
            hvac_mode: HVAC mode to store.

        """
        self._previous_hvac_modes[entity_id] = hvac_mode
        self._schedule_save()

    def clear_previous_hvac_mode(self, entity_id: str) -> None:
        """
        Clear the stored previous HVAC mode for a climate entity.

        Args:
            entity_id: Entity ID of the climate entity.

        """
        self._previous_hvac_modes.pop(entity_id, None)

    def get_cooldown_for_appliance(self, appliance_entity_id: str) -> int:
        """
        Get the cooldown time for a specific appliance.

        Returns the device-specific cooldown if configured,
        otherwise the global cooldown.

        Args:
            appliance_entity_id: Entity ID of the appliance.

        Returns:
            Cooldown time in seconds.

        """
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == appliance_entity_id:
                device_cooldown = sensor_config.get(CONF_DEVICE_COOLDOWN)
                if device_cooldown is not None and device_cooldown > 0:
                    return int(device_cooldown)
                break
        return self._global_cooldown_seconds

    def is_in_cooldown(self, entity_id: str) -> bool:
        """
        Check if an appliance is still in its cooldown period.

        An appliance is in cooldown if it was turned off by the balancer
        less than its configured cooldown time ago.

        Args:
            entity_id: Entity ID of the appliance.

        Returns:
            True if the appliance is still in cooldown, False otherwise.

        """
        if entity_id not in self._balanced_off_appliances:
            return False

        off_info = self._balanced_off_appliances[entity_id]
        off_time = off_info.get("timestamp", 0)
        cooldown = self.get_cooldown_for_appliance(entity_id)
        elapsed = monotonic() - off_time

        if elapsed < cooldown:
            _LOGGER.debug(
                "Appliance %s still in cooldown: %.1f/%.0f seconds elapsed",
                entity_id,
                elapsed,
                cooldown,
            )
            return True
        return False

    def schedule_auto_turn_on(
        self,
        entity_id: str,
        expected_power: float,
        get_total_power_callback: Any,
        get_power_budget_callback: Any,
    ) -> None:
        """
        Schedule an automatic turn-on of an appliance after the configured delay.

        Args:
            entity_id: Entity ID of the appliance.
            expected_power: Expected power consumption in watts.
            get_total_power_callback: Callback to get current total power.
            get_power_budget_callback: Callback returning the effective budget
                in watts, read at restore time so budget changes made during
                the cooldown are honored.

        """
        logger = ContextLogger(_LOGGER, "auto_turn_on").new_operation("schedule")

        try:
            cooldown_seconds = self.get_cooldown_for_appliance(entity_id)

            logger.debug(
                "Scheduling auto turn-on for appliance",
                entity_id=entity_id,
                cooldown_seconds=cooldown_seconds,
            )

            if entity_id in self._scheduled_auto_turn_ons:
                existing_task = self._scheduled_auto_turn_ons[entity_id]
                if not existing_task.done():
                    logger.debug(
                        "Cancelling existing auto turn-on task for %s",
                        entity_id=entity_id,
                    )
                    existing_task.cancel()
                self._scheduled_auto_turn_ons.pop(entity_id)

            self._expected_power_restoration[entity_id] = expected_power
            self._schedule_save()

            async def auto_turn_on_task(
                entity_to_restore: str, delay_seconds: int
            ) -> None:
                try:
                    logger.debug(
                        "Waiting for auto turn-on delay",
                        delay=delay_seconds,
                    )
                    await asyncio.sleep(delay_seconds)

                    logger.debug(
                        "Auto turn-on timer expired", entity_id=entity_to_restore
                    )

                    appliance_state = self.hass.states.get(entity_to_restore)
                    if appliance_state and appliance_state.state != "off":
                        logger.debug(
                            "Appliance is no longer off, cancelling auto turn-on",
                            entity_id=entity_to_restore,
                        )
                        return

                    if self.restore_spacing_remaining() > 0:
                        logger.debug(
                            "Auto turn-on deferred: another restore is settling",
                            entity_id=entity_to_restore,
                        )
                        return

                    current_power = get_total_power_callback()
                    available_budget = (
                        get_power_budget_callback()
                        - current_power
                        - self.settling_power(self.power_of)
                    )

                    expected_power_value = self._expected_power_restoration.get(
                        entity_to_restore, 0.0
                    )

                    logger.debug(
                        "Auto turn-on check",
                        entity_id=entity_to_restore,
                        current_power=current_power,
                        available_budget=available_budget,
                        expected_power=expected_power_value,
                    )

                    margin = get_power_budget_callback() * RESTORE_MARGIN_FRACTION
                    if available_budget - margin < expected_power_value:
                        logger.debug(
                            "Cannot auto turn-on: Insufficient power headroom",
                            entity_id=entity_to_restore,
                            available_budget=available_budget,
                            expected_power=expected_power_value,
                        )
                        return

                    logger.info(
                        "Auto turning on appliance after delay",
                        entity_id=entity_to_restore,
                    )
                    self.note_own_restore(entity_to_restore)
                    try:
                        await self.turn_on_appliance_service(
                            entity_to_restore,
                            f"Automatic restoration after {delay_seconds}s "
                            "power budget timeout",
                        )
                    except BaseException:
                        self.forget_own_restore(entity_to_restore)
                        raise

                    self.note_restore_settling(entity_to_restore, expected_power_value)
                    self._expected_power_restoration.pop(entity_to_restore, None)
                    logger.debug(
                        "Auto turn-on completed successfully",
                        entity_id=entity_to_restore,
                    )

                except Exception as exc:
                    logger.exception(
                        "Auto turn-on task failed", entity_id=entity_to_restore
                    )

                    self._expected_power_restoration.pop(entity_to_restore, None)

                    if self._event_log_sensor:
                        self._event_log_sensor.add_log_entry(
                            f"Auto turn-on failed for {entity_to_restore}. "
                            f"Error: {type(exc).__name__}",
                            category=NOTIFY_PROBLEM,
                        )

            task = self.hass.async_create_background_task(
                auto_turn_on_task(entity_id, cooldown_seconds),
                f"power_load_balancer auto turn-on {entity_id}",
            )
            self._scheduled_auto_turn_ons[entity_id] = task

            logger.debug("Auto turn-on scheduled successfully", entity_id=entity_id)

        except Exception:
            logger.exception("Failed to schedule auto turn-on", entity_id=entity_id)

    def _prepare_turn_off_service(
        self,
        entity_id: str,
        appliance_state: State,
        logger: ContextLogger,
        reason: str,
        domain: str,
    ) -> tuple[str, str, dict[str, str]]:
        """Return the (domain, service, service_data) triple that sheds an appliance."""
        shed_api = self._get_shed_api(entity_id)
        if shed_api is not None:
            logger.debug(
                "Shedding appliance through its managed shed service",
                entity_id=entity_id,
                service=f"{shed_api['domain']}.{shed_api['shed']}",
                reason=reason,
            )
            return (shed_api["domain"], shed_api["shed"], {"entity_id": entity_id})

        if self._is_climate_entity(entity_id):
            service, service_data = self._prepare_climate_turn_off(
                entity_id, appliance_state, logger, reason
            )
            return (domain, service, service_data)
        if self._is_water_heater_entity(entity_id):
            service, service_data = self._prepare_water_heater_turn_off(
                entity_id, appliance_state, logger, reason
            )
            return (domain, service, service_data)
        logger.debug(
            "Turning off appliance",
            entity_id=entity_id,
            reason=reason,
            domain=domain,
        )
        return (domain, "turn_off", {"entity_id": entity_id})

    def _keep_balanced_off_after_failure(
        self, entity_id: str, logger: ContextLogger
    ) -> bool:
        """
        Decide whether a failed turn-off should keep its balanced-off mark.

        A service call can fail (typically on timeout) after the device has
        already switched off. In that case the balanced-off tracking must be
        kept so the appliance is still restored later.
        """
        if self.has_stopped(entity_id):
            logger.warning(
                "Turn-off service call failed but the appliance has stopped; "
                "keeping balanced-off tracking",
                entity_id=entity_id,
            )
            return True
        return False

    @retry_with_backoff(max_retries=2, backoff_factor=0.5, retry_on=(RetryableError,))
    async def turn_off_appliance(self, entity_id: str, reason: str = "") -> None:
        """
        Turn off a specified appliance and record it.

        For climate entities, this sets the HVAC mode to 'off' and stores the
        previous mode for later restoration.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.

        """
        logger = ContextLogger(_LOGGER, "appliance").new_operation("turn_off")

        try:
            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if not self._is_appliance_active(entity_id, appliance_state.state):
                logger.debug(
                    "Appliance is not in an active state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return

            domain = entity_id.split(".", maxsplit=1)[0]
            call_domain, service, service_data = self._prepare_turn_off_service(
                entity_id, appliance_state, logger, reason, domain
            )

            params = ServiceCallParams(
                hass=self.hass,
                domain=call_domain,
                service=service,
                service_data=service_data,
                logger=logger,
            )

            self.mark_appliance_balanced_off(entity_id, reason)
            try:
                await safe_service_call(params)
            except Exception:
                if not self._keep_balanced_off_after_failure(entity_id, logger):
                    self.remove_from_balanced_off(entity_id)
                    raise

            logger.info(
                "Successfully turned off appliance", entity_id=entity_id, reason=reason
            )

            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Turned off {entity_id}. Reason: {reason}",
                    category=NOTIFY_SHED,
                )

        except (EntityNotFoundError, EntityUnavailableError) as exc:
            logger.exception(
                "Appliance entity issue", entity_id=entity_id, error=str(exc)
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                appliance_control_issue_id(entity_id),
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE,
                translation_placeholders={"entity_id": entity_id},
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Failed to turn off {entity_id}. Error: {exc.error_code}",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Cannot control appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except (ServiceCallError, ServiceTimeoutError) as exc:
            logger.exception("Service call failed", entity_id=entity_id, error=str(exc))
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Failed to turn off {entity_id}. Error: Service call failed",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Failed to turn off appliance {entity_id}: {exc}"
            raise RetryableError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except Exception as exc:
            logger.exception(
                "Unexpected error turning off appliance", entity_id=entity_id
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Failed to turn off {entity_id}. Error: {type(exc).__name__}",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Unexpected error turning off appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "error": str(exc)},
            ) from exc

    async def turn_off_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> None:
        """
        Handle the turn_off_appliance service call.

        For climate entities, this sets the HVAC mode to 'off' and stores the
        previous mode for later restoration.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.
            context: Optional Home Assistant context.

        """
        logger = ContextLogger(_LOGGER, "service").new_operation(
            "turn_off_appliance_service"
        )

        try:
            logger.debug(
                "Service turn_off_appliance called",
                entity_id=entity_id,
                reason=reason,
                context_id=str(context.id) if context else None,
            )

            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if not self._is_appliance_active(entity_id, appliance_state.state):
                logger.debug(
                    "Appliance is not in an active state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return

            domain = entity_id.split(".", maxsplit=1)[0]
            call_domain, service, service_data = self._prepare_turn_off_service(
                entity_id, appliance_state, logger, reason, domain
            )

            params = ServiceCallParams(
                hass=self.hass,
                domain=call_domain,
                service=service,
                service_data=service_data,
                logger=logger,
            )
            await safe_service_call(params)

            self.hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": "Power Load Balancer",
                    "message": f"turned off {entity_id}"
                    + (f": {reason}" if reason else ""),
                    "domain": DOMAIN,
                    "entity_id": entity_id,
                    "context_id": str(context.id) if context else None,
                },
                context=context,
            )

            logger.info(
                "Successfully turned off appliance via service",
                entity_id=entity_id,
                reason=reason,
            )

            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call: Turned off {entity_id}. Reason: {reason}",
                    category=NOTIFY_SHED,
                )

        except (EntityNotFoundError, EntityUnavailableError) as exc:
            logger.exception(
                "Service call failed - entity issue",
                entity_id=entity_id,
                error=str(exc),
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                appliance_control_issue_id(entity_id),
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE,
                translation_placeholders={"entity_id": entity_id},
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Cannot turn off {entity_id}. "
                    f"Error: {exc.error_code}",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Cannot control appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except (ServiceCallError, ServiceTimeoutError) as exc:
            logger.exception("Service call failed", entity_id=entity_id, error=str(exc))
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Turn off {entity_id}. "
                    "Error: Service call failed",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Failed to turn off appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except Exception as exc:
            logger.exception("Unexpected error in service call", entity_id=entity_id)
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Turn off {entity_id}. "
                    f"Error: {type(exc).__name__}",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Unexpected error turning off appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "error": str(exc)},
            ) from exc

    async def turn_on_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> bool:
        """
        Handle the turn_on_appliance service call.

        For climate entities, this restores the previously stored HVAC mode.
        If no previous mode is stored, it defaults to 'heat'.

        Args:
            entity_id: Entity ID of the appliance to turn on.
            reason: Reason for turning on the appliance.
            context: Optional Home Assistant context.

        Returns:
            False when nothing could be done to turn it on, so the caller
            must keep holding it rather than forget it; True otherwise.

        """
        logger = ContextLogger(_LOGGER, "service").new_operation(
            "turn_on_appliance_service"
        )

        try:
            logger.debug(
                "Service turn_on_appliance called",
                entity_id=entity_id,
                reason=reason,
                context_id=str(context.id) if context else None,
            )

            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if not self._is_appliance_off(entity_id, appliance_state.state):
                logger.debug(
                    "Appliance is not in 'off' state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return True

            prepared_service_call = self._prepare_turn_on_service_call(
                entity_id,
                appliance_state,
                reason,
                logger,
            )
            if prepared_service_call is None:
                return False

            domain, service, service_data = prepared_service_call

            params = ServiceCallParams(
                hass=self.hass,
                domain=domain,
                service=service,
                service_data=service_data,
                logger=logger,
            )
            await safe_service_call(params)

            if self._is_climate_entity(entity_id):
                self.clear_previous_hvac_mode(entity_id)
            if self._previous_operation_modes.pop(entity_id, None) is not None:
                self._schedule_save()

            self.hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": "Power Load Balancer",
                    "message": f"turned on {entity_id}"
                    + (f": {reason}" if reason else ""),
                    "domain": DOMAIN,
                    "entity_id": entity_id,
                    "context_id": str(context.id) if context else None,
                },
                context=context,
            )

            logger.info(
                "Successfully turned on appliance via service",
                entity_id=entity_id,
                reason=reason,
            )

            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call: Turned on {entity_id}. Reason: {reason}",
                    category=NOTIFY_RESTORE,
                )

            return True  # noqa: TRY300

        except (EntityNotFoundError, EntityUnavailableError) as exc:
            logger.exception(
                "Service call failed - entity issue",
                entity_id=entity_id,
                error=str(exc),
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Cannot turn on {entity_id}. "
                    f"Error: {exc.error_code}",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Cannot control appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except (ServiceCallError, ServiceTimeoutError) as exc:
            logger.exception("Service call failed", entity_id=entity_id, error=str(exc))
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Turn on {entity_id}. "
                    "Error: Service call failed",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Failed to turn on appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except Exception as exc:
            logger.exception("Unexpected error in service call", entity_id=entity_id)
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Turn on {entity_id}. "
                    f"Error: {type(exc).__name__}",
                    category=NOTIFY_PROBLEM,
                )
            msg = f"Unexpected error turning on appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "error": str(exc)},
            ) from exc

    def cleanup(self) -> None:
        """Clean up the controller and cancel all scheduled tasks."""
        for task in self._scheduled_auto_turn_ons.values():
            if not task.done():
                task.cancel()
        self._scheduled_auto_turn_ons.clear()
        self._expected_power_restoration.clear()
        self._balanced_off_appliances.clear()
        self._previous_hvac_modes.clear()
        self._previous_operation_modes.clear()
        self._own_restores.clear()
        self._settling.clear()
        self._last_restore_at = None

    def get_diagnostics_snapshot(self) -> dict[str, Any]:
        """Return runtime diagnostics data for troubleshooting."""
        scheduled_tasks = {
            entity_id: {
                "done": task.done(),
                "cancelled": task.cancelled(),
            }
            for entity_id, task in self._scheduled_auto_turn_ons.items()
        }

        return {
            "balanced_off_appliances": dict(self._balanced_off_appliances),
            "expected_power_restoration_watt": dict(self._expected_power_restoration),
            "scheduled_auto_turn_on_tasks": scheduled_tasks,
            "stored_previous_hvac_modes": dict(self._previous_hvac_modes),
            "learned_draw": self.learner.diagnostics(),
            "settling_restores": {
                entity_id: {
                    "watts": hold["watts"],
                    "age_seconds": round(monotonic() - hold["since"]),
                }
                for entity_id, hold in self._settling.items()
            },
        }
