"""
Core balancing logic for the Power Load Balancer integration.

This module contains the PowerLoadBalancer class which manages power monitoring,
appliance control, and automatic load balancing based on a configured power budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import Context, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.util import dt

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

from .appliance_controller import ApplianceController
from .balancing_engine import BalancingCallbacks, BalancingEngine
from .clock import monotonic
from .const import (
    CONF_APPLIANCE,
    CONF_COOLDOWN_SECONDS,
    CONF_MAIN_POWER_SENSOR,
    CONF_NOTIFY_PERSISTENT,
    CONF_NOTIFY_SERVICE,
    CONF_POWER_BUDGET_WATT,
    CONF_POWER_SENSORS,
    CONF_SUSTAINED_DURATION_SECONDS,
    CONF_SUSTAINED_ENABLED,
    CONF_SUSTAINED_THRESHOLD_PERCENT,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_NOTIFY_PERSISTENT,
    DEFAULT_NOTIFY_SERVICE,
    DEFAULT_SUSTAINED_DURATION_SECONDS,
    DEFAULT_SUSTAINED_ENABLED,
    DEFAULT_SUSTAINED_THRESHOLD_PERCENT,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    ISSUE_TRANSLATION_KEY_DEVICE_NOT_CONTROLLABLE,
    ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE,
    NON_BINARY_ACTIVE_STATE_DOMAINS,
    SHED_VERIFY_GRACE_SECONDS,
    SUSTAINED_DISARM_GRACE_SECONDS,
    SUSTAINED_ESCALATION_DWELL_SECONDS,
    SUSTAINED_ESCALATION_WINDOW_SECONDS,
)
from .context_logger import ContextLogger
from .event_dispatcher import BalancerEventDispatcher
from .exceptions import ConfigurationError
from .power_monitor import PowerMonitor

_LOGGER = logging.getLogger(__name__)
AVAILABILITY_EVENT_HISTORY_SIZE = 100
UNAVAILABLE_ENTITY_ISSUE_PREFIX = "balancing_entity_unavailable"
NON_CONTROLLABLE_ENTITY_ISSUE_PREFIX = "balancing_entity_not_controllable"


class PowerLoadBalancer:
    """
    Core class for managing power load balancing.

    This class coordinates the power monitor, appliance controller, and balancing engine
    to automatically turn off appliances when power exceeds the budget and restore them
    when power headroom allows.

    Attributes:
        hass: Home Assistant instance.
        entry: Configuration entry for this integration.

    """

    hass: HomeAssistant
    entry: ConfigEntry
    _config_data: dict[str, Any]
    _event_log_sensor: Any
    _device_id: str | None
    _main_power_sensor_entity_id: str
    _monitored_sensors: list[dict[str, Any]]
    _power_budget: int
    _main_power_sensor_unsub: Callable[[], None] | None
    _monitored_sensors_unsub: Callable[[], None] | None
    _appliance_unsub: Callable[[], None] | None
    _power_monitor: PowerMonitor
    _appliance_controller: ApplianceController
    _balancing_engine: BalancingEngine
    _was_over_budget: bool
    _sustained_enabled: bool
    _sustained_threshold_percent: int
    _sustained_duration_seconds: int
    _sustained_since: float | None
    _last_sustained_shed_at: float | None
    _sustained_check_unsub: Callable[[], None] | None
    _reported_sustained_failure: bool
    _below_arm_since: float | None
    _unavailable_entities: dict[str, dict[str, Any]]
    _availability_events: list[dict[str, Any]]
    _non_controllable_media_players: set[str]
    _enabled: bool
    _skip_reload: bool

    def __init__(
        self,
        hass: HomeAssistant,
        config_data: dict[str, Any],
        entry: ConfigEntry,
    ) -> None:
        """
        Initialize the PowerLoadBalancer.

        Args:
            hass: Home Assistant instance.
            config_data: Configuration data dictionary.
            entry: Configuration entry for this integration.

        """
        self.hass = hass
        self.entry = entry
        self._config_data = config_data
        self._event_log_sensor = None
        self._device_id = None
        self._main_power_sensor_unsub = None
        self._monitored_sensors_unsub = None
        self._appliance_unsub = None
        self._was_over_budget = False
        self._unavailable_entities = {}
        self._availability_events = []
        self._non_controllable_media_players = set()
        self._enabled = True
        self._skip_reload = False

        self._main_power_sensor_entity_id = config_data[CONF_MAIN_POWER_SENSOR]
        self._monitored_sensors = config_data.get(CONF_POWER_SENSORS, [])
        self._power_budget = config_data[CONF_POWER_BUDGET_WATT]
        self._global_cooldown_seconds = config_data.get(
            CONF_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS
        )
        self._sustained_enabled = bool(
            config_data.get(CONF_SUSTAINED_ENABLED, DEFAULT_SUSTAINED_ENABLED)
        )
        self._sustained_threshold_percent = int(
            config_data.get(
                CONF_SUSTAINED_THRESHOLD_PERCENT,
                DEFAULT_SUSTAINED_THRESHOLD_PERCENT,
            )
        )
        self._sustained_duration_seconds = int(
            config_data.get(
                CONF_SUSTAINED_DURATION_SECONDS,
                DEFAULT_SUSTAINED_DURATION_SECONDS,
            )
        )
        self._sustained_since = None
        self._last_sustained_shed_at = None
        self._sustained_check_unsub = None
        self._reported_sustained_failure = False
        self._below_arm_since = None
        self._unverified_sheds: set[str] = set()
        self._event_dispatcher = BalancerEventDispatcher(
            hass,
            config_data["entry_id"],
            notify_persistent=bool(
                config_data.get(CONF_NOTIFY_PERSISTENT, DEFAULT_NOTIFY_PERSISTENT)
            ),
            notify_service=str(
                config_data.get(CONF_NOTIFY_SERVICE, DEFAULT_NOTIFY_SERVICE)
            ),
        )

        self._power_monitor = PowerMonitor(
            hass,
            self._main_power_sensor_entity_id,
            self._monitored_sensors,
            self._power_budget,
        )
        self._appliance_controller = ApplianceController(
            hass,
            self._monitored_sensors,
            self._global_cooldown_seconds,
            self._event_dispatcher,
        )
        self._balancing_engine = BalancingEngine(
            hass, self._monitored_sensors, self._power_budget
        )
        self._apply_effective_budget()

        monitored_sensor_ids = [
            sensor.get(CONF_ENTITY_ID, "unknown") for sensor in self._monitored_sensors
        ]
        _LOGGER.info(
            "PowerLoadBalancer initialized with monitored sensors: %s",
            monitored_sensor_ids,
        )

    async def async_setup(self) -> None:
        """
        Set up the listeners and create entities.

        Initializes state tracking, registers event listeners for power sensors
        and appliances, and creates the device registry entry.
        """
        _LOGGER.debug("Setting up PowerLoadBalancer listeners and entities")

        device_registry = async_get_device_registry(self.hass)
        device_entry = device_registry.async_get_or_create(
            config_entry_id=self._config_data["entry_id"],
            identifiers={(DOMAIN, self._config_data["entry_id"])},
            name="Power Load Balancer",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
        )
        self._device_id = getattr(device_entry, "id", None)

        self._power_monitor.initialize_power_tracking()

        self._initialize_availability_tracking()
        self._initialize_controllability_tracking()
        self._adopt_appliances_already_shed()

        _LOGGER.debug("PowerLoadBalancer setup complete.")

    def _adopt_appliances_already_shed(self) -> None:
        """
        Take back ownership of appliances still reporting themselves shed.

        Which appliances the balancer had shed is held in memory, but the shed
        itself lives in the appliance and survives a restart or a reload of
        this entry. Without this the two drift apart: the appliance stays
        suppressed while the balancer no longer knows it can restore it, so
        nothing ever releases it.
        """
        for sensor_config in self._monitored_sensors:
            entity_id = sensor_config.get(CONF_APPLIANCE)
            if not isinstance(entity_id, str):
                continue
            if not self._appliance_controller.is_appliance_shed(entity_id):
                continue

            self._appliance_controller.mark_appliance_balanced_off(
                entity_id, "adopted at startup: appliance reports it is shed"
            )
            _LOGGER.info(
                "Adopted %s: it reports itself shed, so the balancer will "
                "restore it when there is headroom",
                entity_id,
            )

    @callback
    def async_start_listening(self) -> None:
        """
        Register the state-change listeners that drive balancing.

        Kept separate from async_setup so the entity platforms (and the
        Enabled switch's restored state in particular) are fully set up
        before any event can trigger a shed. Without this split, a balancer
        the user had disabled could shed during the startup window between
        listener registration and the switch restoring its 'off' state.
        """
        self._main_power_sensor_unsub = async_track_state_change_event(
            self.hass,
            self._main_power_sensor_entity_id,
            self._handle_power_sensor_state_change,
        )

        monitored_sensor_entity_ids = [
            s[CONF_ENTITY_ID] for s in self._monitored_sensors
        ]
        if monitored_sensor_entity_ids:
            self._monitored_sensors_unsub = async_track_state_change_event(
                self.hass,
                monitored_sensor_entity_ids,
                self._handle_power_sensor_state_change,
            )

        appliance_entity_ids = [s[CONF_APPLIANCE] for s in self._monitored_sensors]
        if appliance_entity_ids:
            self._appliance_unsub = async_track_state_change_event(
                self.hass,
                appliance_entity_ids,
                self._handle_appliance_state_change,
            )

        _LOGGER.debug("PowerLoadBalancer listeners registered.")

    def _record_availability_event(self, event: dict[str, Any]) -> None:
        """Record an availability event for diagnostics history."""
        self._availability_events.append(event)
        if len(self._availability_events) > AVAILABILITY_EVENT_HISTORY_SIZE:
            self._availability_events = self._availability_events[
                -AVAILABILITY_EVENT_HISTORY_SIZE:
            ]

    def _get_unavailable_issue_id(self, entity_id: str) -> str:
        """Return a stable issue ID for an unavailable balancing entity."""
        sanitized_entity = entity_id.replace(".", "_")
        return (
            f"{UNAVAILABLE_ENTITY_ISSUE_PREFIX}_{self.entry.entry_id}_"
            f"{sanitized_entity}"
        )

    def _get_non_controllable_issue_id(self, entity_id: str) -> str:
        """Return a stable issue ID for a non-controllable appliance."""
        sanitized_entity = entity_id.replace(".", "_")
        return (
            f"{NON_CONTROLLABLE_ENTITY_ISSUE_PREFIX}_{self.entry.entry_id}_"
            f"{sanitized_entity}"
        )

    def _mark_entity_unavailable(
        self, entity_id: str, entity_type: str, state: str | None
    ) -> None:
        """Track when an entity becomes unavailable for balancing."""
        now_iso = dt.utcnow().isoformat(timespec="seconds")
        if entity_id in self._unavailable_entities:
            self._unavailable_entities[entity_id]["last_seen"] = now_iso
            self._unavailable_entities[entity_id]["state"] = state
            return

        unavailable_info = {
            "entity": entity_id,
            "entity_type": entity_type,
            "state": state,
            "first_seen": now_iso,
            "last_seen": now_iso,
        }
        self._unavailable_entities[entity_id] = unavailable_info

        self._record_availability_event(
            {
                "timestamp": now_iso,
                "event": "became_unavailable",
                "entity": entity_id,
                "entity_type": entity_type,
                "state": state,
                "reason": "entity unavailable for balancing",
            }
        )

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._get_unavailable_issue_id(entity_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE,
            translation_placeholders={"entity_id": entity_id},
        )

        _LOGGER.warning(
            "Entity %s became unavailable for balancing (type=%s, state=%s)",
            entity_id,
            entity_type,
            state,
        )

    def _mark_entity_available(
        self, entity_id: str, entity_type: str, state: str | None
    ) -> None:
        """Track when an entity becomes available again for balancing."""
        unavailable_info = self._unavailable_entities.pop(entity_id, None)
        if unavailable_info is None:
            return

        now_iso = dt.utcnow().isoformat(timespec="seconds")
        self._record_availability_event(
            {
                "timestamp": now_iso,
                "event": "restored",
                "entity": entity_id,
                "entity_type": entity_type,
                "state": state,
                "unavailable_since": unavailable_info.get("first_seen"),
                "reason": "entity restored for balancing",
            }
        )

        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            self._get_unavailable_issue_id(entity_id),
        )

        _LOGGER.info(
            "Entity %s is available again for balancing (type=%s, state=%s)",
            entity_id,
            entity_type,
            state,
        )

    def _initialize_availability_tracking(self) -> None:
        """Capture initial availability of all entities relevant to balancing."""
        tracked_entities: list[tuple[str, str]] = [
            (self._main_power_sensor_entity_id, "main_power_sensor"),
        ]
        tracked_entities.extend(
            (sensor_config[CONF_ENTITY_ID], "power_sensor")
            for sensor_config in self._monitored_sensors
        )
        tracked_entities.extend(
            (sensor_config[CONF_APPLIANCE], "appliance")
            for sensor_config in self._monitored_sensors
        )

        for entity_id, entity_type in tracked_entities:
            state = self.hass.states.get(entity_id)
            state_value = state.state if state is not None else None
            if state is None or state_value in ("unknown", "unavailable"):
                self._mark_entity_unavailable(entity_id, entity_type, state_value)

    def _clear_unavailable_entity_issues(self) -> None:
        """Delete all outstanding Repairs issues for unavailable entities."""
        for entity_id in list(self._unavailable_entities):
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                self._get_unavailable_issue_id(entity_id),
            )

    def _clear_non_controllable_entity_issues(self) -> None:
        """Delete all outstanding Repairs issues for non-controllable entities."""
        for entity_id in list(self._non_controllable_media_players):
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                self._get_non_controllable_issue_id(entity_id),
            )

    def _is_media_player_controllable(self, state: Any) -> bool:
        """Return True if a media player supports turn on/off."""
        supported_features = state.attributes.get("supported_features", 0)
        return bool(
            supported_features
            & (MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF)
        )

    def _update_media_player_controllability(self, entity_id: str, state: Any) -> None:
        """Create or clear repairs for non-controllable media players."""
        if not entity_id.startswith("media_player."):
            return

        if state is None:
            return

        if self._is_media_player_controllable(state):
            if entity_id in self._non_controllable_media_players:
                ir.async_delete_issue(
                    self.hass,
                    DOMAIN,
                    self._get_non_controllable_issue_id(entity_id),
                )
                self._non_controllable_media_players.discard(entity_id)
            return

        if entity_id in self._non_controllable_media_players:
            return

        self._non_controllable_media_players.add(entity_id)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._get_non_controllable_issue_id(entity_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TRANSLATION_KEY_DEVICE_NOT_CONTROLLABLE,
            translation_placeholders={"entity_id": entity_id},
        )

    def _initialize_controllability_tracking(self) -> None:
        """Create initial Repairs issues for non-controllable media players."""
        for sensor_config in self._monitored_sensors:
            entity_id = sensor_config.get(CONF_APPLIANCE)
            if not isinstance(entity_id, str):
                continue
            state = self.hass.states.get(entity_id)
            self._update_media_player_controllability(entity_id, state)

    async def async_cleanup(self) -> None:
        """
        Clean up the PowerLoadBalancer and unsubscribe from events.

        Cancels all scheduled tasks and clears internal state.
        """
        logger = ContextLogger(_LOGGER, "cleanup").new_operation(
            "power_balancer_cleanup"
        )

        try:
            logger.debug("Starting PowerLoadBalancer cleanup")

            if self._main_power_sensor_unsub:
                logger.debug("Unsubscribing from main power sensor events")
                self._main_power_sensor_unsub()
                self._main_power_sensor_unsub = None

            if self._monitored_sensors_unsub:
                logger.debug("Unsubscribing from monitored sensor events")
                self._monitored_sensors_unsub()
                self._monitored_sensors_unsub = None

            if self._appliance_unsub:
                logger.debug("Unsubscribing from appliance events")
                self._appliance_unsub()
                self._appliance_unsub = None

            self._enabled = False
            self._cancel_sustained_check()
            self._event_dispatcher.async_dismiss_persistent()
            self._power_monitor.clear_tracking()
            self._appliance_controller.cleanup()
            self._clear_unavailable_entity_issues()
            self._clear_non_controllable_entity_issues()
            self._unavailable_entities.clear()
            self._availability_events.clear()
            self._non_controllable_media_players.clear()
            self._event_log_sensor = None

            logger.info("PowerLoadBalancer cleanup completed successfully")

        except Exception as exc:
            logger.exception("Error during PowerLoadBalancer cleanup")
            msg = f"Failed to cleanup PowerLoadBalancer: {exc}"
            raise ConfigurationError(
                msg,
                details={"error": str(exc)},
            ) from exc

    @property
    def device_id(self) -> str | None:
        """Return the device ID."""
        return self._device_id

    @property
    def enabled(self) -> bool:
        """Return whether the balancer is enabled."""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Set the enabled state of the balancer."""
        self._enabled = enabled
        self._reset_sustained_tracking()
        _LOGGER.info("Power Load Balancer %s", "enabled" if enabled else "disabled")

    def _reset_sustained_tracking(self) -> None:
        """Fully reset the sustained-load tracking state."""
        self._sustained_since = None
        self._last_sustained_shed_at = None
        self._reported_sustained_failure = False
        self._below_arm_since = None
        self._unverified_sheds.clear()
        self._cancel_sustained_check()

    def _cancel_sustained_check(self) -> None:
        """Cancel a pending time-based sustained-load re-check, if any."""
        if self._sustained_check_unsub is not None:
            self._sustained_check_unsub()
            self._sustained_check_unsub = None

    def _schedule_sustained_check(self, delay_seconds: float) -> None:
        """
        Schedule a time-based re-evaluation of the sustained-load state.

        State-change events alone cannot expire the dwell timer when the
        main sensor reading flat-lines (no state change means no event), so
        whenever the tracker is armed a wall-clock callback re-runs the
        balance check shortly after the dwell would elapse.
        """
        self._cancel_sustained_check()
        self._sustained_check_unsub = async_call_later(
            self.hass,
            max(delay_seconds, 1.0),
            self._handle_sustained_check_timer,
        )

    @callback
    def _handle_sustained_check_timer(self, _now: Any) -> None:
        """Re-run the balance check when the sustained dwell may have elapsed."""
        self._sustained_check_unsub = None
        self.async_check_and_balance()

    @property
    def power_budget(self) -> int:
        """Return the current power budget."""
        return self._power_budget

    @property
    def effective_budget(self) -> float:
        """
        Return the operating ceiling the balancer keeps the load under.

        With sustained-load shedding enabled this is the configured
        percentage of the power budget; otherwise it equals the budget.
        Turn-on vetoes and restorations are all evaluated against this
        value so the balancer never restores an appliance into a load
        level that would immediately re-trigger shedding.
        """
        if self._sustained_enabled:
            return self._power_budget * self._sustained_threshold_percent / 100.0
        return float(self._power_budget)

    def _apply_effective_budget(self) -> None:
        """Propagate the current effective budget to sub-components."""
        effective_budget = self.effective_budget
        self._power_monitor.set_effective_budget(effective_budget)
        self._balancing_engine.set_effective_budget(effective_budget)

    def set_power_budget(self, budget: int) -> None:
        """Set the power budget and propagate to sub-components."""
        if budget == self._power_budget:
            return
        old_budget = self._power_budget
        self._power_budget = budget
        self._power_monitor.set_power_budget(budget)
        self._balancing_engine.set_power_budget(budget)
        self._apply_effective_budget()
        _LOGGER.info(
            "Power budget updated to %s W (effective budget %s W)",
            budget,
            round(self.effective_budget),
        )
        self._event_dispatcher.add_log_entry(
            f"Power budget changed from {old_budget} W to {budget} W "
            f"(effective {round(self.effective_budget)} W)"
        )

    @property
    def skip_reload(self) -> bool:
        """Return whether the next config update should skip reload."""
        return self._skip_reload

    @skip_reload.setter
    def skip_reload(self, value: bool) -> None:
        """Set whether the next config update should skip reload."""
        self._skip_reload = value

    async def async_disable_and_restore(self) -> None:
        """Disable balancing and restore all balanced-off appliances."""
        self._enabled = False
        self._reset_sustained_tracking()
        _LOGGER.info(
            "Power Load Balancer disabled, restoring all balanced-off appliances"
        )

        balanced_off = self._appliance_controller.get_balanced_off_appliances()
        for entity_id in balanced_off:
            self._appliance_controller.cancel_scheduled_turn_on(entity_id)
            try:
                await self._appliance_controller.turn_on_appliance_service(
                    entity_id, "Balancer disabled - restoring appliance"
                )
            except Exception:
                _LOGGER.exception(
                    "Failed to restore appliance %s during disable", entity_id
                )
            self._appliance_controller.remove_from_balanced_off(entity_id)

    def register_event_log_sensor(self, sensor: Any) -> None:
        """
        Register the event log sensor instance.

        Args:
            sensor: The PowerBalancerLogSensor instance to register.

        """
        self._event_log_sensor = sensor
        self._event_dispatcher.set_sensor(sensor)

    async def _handle_power_sensor_state_change(self, event: Any) -> None:
        """
        Handle state changes for power sensors triggered by Home Assistant events.

        Args:
            event: The state change event from Home Assistant.

        """
        entity_id = event.data.get("entity_id") if hasattr(event, "data") else None
        new_state = event.data.get("new_state") if hasattr(event, "data") else None

        if isinstance(entity_id, str):
            is_main_sensor = entity_id == self._main_power_sensor_entity_id
            entity_type = "main_power_sensor" if is_main_sensor else "power_sensor"
            state_value = new_state.state if new_state is not None else None

            if new_state is None or state_value in ("unknown", "unavailable"):
                self._mark_entity_unavailable(entity_id, entity_type, state_value)
            else:
                self._mark_entity_available(entity_id, entity_type, state_value)

        await self._power_monitor.handle_power_sensor_state_change(
            event, self.async_check_and_balance
        )

    async def _handle_appliance_state_change(self, event: Any) -> None:
        """Handle state changes for controllable appliances."""
        entity_id = event.data.get("entity_id")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        state_value = new_state.state if new_state is not None else None

        if new_state is None or state_value in ("unknown", "unavailable"):
            if isinstance(entity_id, str):
                self._mark_entity_unavailable(entity_id, "appliance", state_value)
            _LOGGER.debug("Ignoring invalid state for appliance %s", entity_id)
            return

        if isinstance(entity_id, str):
            self._mark_entity_available(entity_id, "appliance", state_value)
            self._update_media_player_controllability(entity_id, new_state)

        _LOGGER.debug(
            "Appliance %s state changed from %s to %s",
            entity_id,
            old_state.state if old_state else None,
            new_state.state,
        )

        is_now_active = self._state_is_drawing(entity_id, new_state)
        was_active = self._state_is_drawing(entity_id, old_state)

        if is_now_active and not was_active:
            await self._handle_appliance_turn_on(entity_id)
        elif not is_now_active and was_active:
            self._handle_appliance_turn_off(entity_id)

        self.async_check_and_balance()

    async def _handle_appliance_turn_on(self, entity_id: str) -> None:
        """Handle an appliance being turned on."""
        self._appliance_controller.remove_from_balanced_off(entity_id)
        self._appliance_controller.cancel_scheduled_turn_on(entity_id)

        if not self._enabled:
            return

        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == entity_id:
                power_to_add = self._power_monitor.calculate_sensor_power(
                    sensor_config
                ) or self._appliance_controller.get_nominal_power(entity_id)

                if self._power_monitor.would_exceed_budget(power_to_add):
                    await self._appliance_controller.turn_off_appliance(
                        entity_id,
                        reason=(
                            f"Power {power_to_add} W would exceed the "
                            f"effective budget of "
                            f"{round(self.effective_budget)} W"
                        ),
                    )
                    return

                self._power_monitor.update_power_estimates(sensor_config, power_to_add)
                self.async_check_and_balance()
                break

    def _handle_appliance_turn_off(self, entity_id: str) -> None:
        """Handle an appliance being turned off."""
        sensor_id = self._appliance_controller.get_sensor_for_appliance(entity_id)

        if (
            not self._appliance_controller.is_appliance_balanced_off(entity_id)
            and sensor_id
        ):
            self._power_monitor.remove_sensor_power(sensor_id)

    @callback
    def get_total_house_power(self) -> float:
        """Return the current total house power as measured by the main power sensor."""
        return self._power_monitor.get_total_house_power()

    @callback
    def async_check_and_balance(self) -> None:
        """Check power usage and perform balancing if necessary."""
        if not self._enabled:
            return

        self._verify_sheds_took_effect()

        current_total_power = self.get_total_house_power()
        is_over_budget = current_total_power > self._power_budget
        sustained_shed_due = self._update_sustained_state(current_total_power)

        _LOGGER.debug(
            "Checking balance: Current total power = %s W, Budget = %s W, "
            "Effective budget = %s W",
            current_total_power,
            self._power_budget,
            self.effective_budget,
        )

        if is_over_budget:
            if not self._was_over_budget:
                _LOGGER.warning(
                    "Total power %s W exceeds budget %s W. Initiating balancing.",
                    current_total_power,
                    self._power_budget,
                )
            else:
                _LOGGER.debug(
                    "Total power %s W remains above budget %s W.",
                    current_total_power,
                    self._power_budget,
                )
            shed_initiated = self._balance_down()
            if shed_initiated and self._sustained_since is not None:
                self._record_sustained_shed(monotonic())
        elif sustained_shed_due:
            now = monotonic()
            sustained_seconds = (
                round(now - self._sustained_since)
                if self._sustained_since is not None
                else 0
            )
            shed_initiated = self._balance_down(
                f"Sustained load: at or above {round(self.effective_budget)} W "
                f"({self._sustained_threshold_percent}% of "
                f"{self._power_budget} W budget) for {sustained_seconds} s"
            )
            if shed_initiated:
                _LOGGER.warning(
                    "Sustained load: %s W has stayed at or above the effective "
                    "budget of %s W (%s%% of %s W) for %s s. Shedding.",
                    current_total_power,
                    round(self.effective_budget),
                    self._sustained_threshold_percent,
                    self._power_budget,
                    sustained_seconds,
                )
                self._reported_sustained_failure = False
                self._record_sustained_shed(now)
            else:
                if not self._reported_sustained_failure:
                    _LOGGER.warning(
                        "Sustained load: %s W at or above the effective budget "
                        "of %s W for %s s, but no appliance is available to "
                        "shed.",
                        current_total_power,
                        round(self.effective_budget),
                        sustained_seconds,
                    )
                    self._event_dispatcher.add_log_entry(
                        f"Unable to shed: {round(current_total_power)} W at or "
                        f"above the effective budget of "
                        f"{round(self.effective_budget)} W with no appliance "
                        f"available"
                    )
                    self._reported_sustained_failure = True
                self._sustained_since = now
                self._schedule_sustained_check(self._current_dwell_seconds(now))
        else:
            if self._was_over_budget:
                _LOGGER.info(
                    "Power returned within budget: %s W <= %s W",
                    current_total_power,
                    self._power_budget,
                )
            _LOGGER.debug(
                "Total power %s W is within budget %s W.",
                current_total_power,
                self._power_budget,
            )
            self._balance_up()

        self._was_over_budget = is_over_budget

        if (
            self._sustained_enabled
            and self._sustained_since is not None
            and self._sustained_check_unsub is None
        ):
            now = monotonic()
            remaining = self._current_dwell_seconds(now) - (now - self._sustained_since)
            self._schedule_sustained_check(remaining)

    def _verify_sheds_took_effect(self) -> None:
        """
        Report appliances that were shed but are still drawing.

        A service call can succeed while the appliance keeps running: an
        integration may refuse the request, or a device may ignore it. The
        balancer would then hold the appliance as shed and never offer that
        rung of the ladder again, while the load it was supposed to free is
        still there. The load reading itself is authoritative and self-heals,
        so this surfaces the discrepancy rather than acting on it.
        """
        for entity_id in self._appliance_controller.get_balanced_off_appliances():
            age = self._appliance_controller.get_balanced_off_age(entity_id)
            if age is None or age < SHED_VERIFY_GRACE_SECONDS:
                continue

            state = self.hass.states.get(entity_id)
            if not self._state_is_drawing(entity_id, state):
                self._unverified_sheds.discard(entity_id)
                continue

            if entity_id in self._unverified_sheds:
                continue

            self._unverified_sheds.add(entity_id)
            _LOGGER.warning(
                "Shed of %s did not take effect: still reporting %s after %s s",
                entity_id,
                state.state,
                round(age),
            )
            self._event_dispatcher.add_log_entry(
                f"Shed of {entity_id} did not take effect: still {state.state} "
                f"after {round(age)} s"
            )

    def _state_is_drawing(self, entity_id: str, state: Any) -> bool:
        """
        Return True when a state snapshot means the appliance is drawing.

        An appliance suppressed through a managed shed keeps reporting the
        operation mode its owner chose, so the reported mode alone cannot
        separate running from shed -- and a shed being cleared by hand would
        otherwise look like no change at all.
        """
        if state is None:
            return False
        if self._appliance_controller.state_reports_shed(entity_id, state):
            return False
        if entity_id.startswith(
            tuple(f"{domain}." for domain in NON_BINARY_ACTIVE_STATE_DOMAINS)
        ):
            return state.state not in ("off", "unknown", "unavailable")
        return state.state == "on"

    def _record_sustained_shed(self, now: float) -> None:
        """
        Restart the dwell clock after a shed while the load is still at level.

        Any shed (budget-triggered or sustained-triggered) resets the dwell
        so the next rung of the ladder is only shed after the escalation
        dwell has passed with the load still at or above the effective
        budget — never immediately on the next sample.
        """
        self._last_sustained_shed_at = now
        self._sustained_since = now
        self._schedule_sustained_check(self._current_dwell_seconds(now))

    def _current_dwell_seconds(self, now: float) -> float:
        """Return the dwell that currently applies before the next shed."""
        dwell_seconds = float(self._sustained_duration_seconds)
        if (
            self._last_sustained_shed_at is not None
            and now - self._last_sustained_shed_at
            <= SUSTAINED_ESCALATION_WINDOW_SECONDS
        ):
            dwell_seconds = min(
                dwell_seconds, float(SUSTAINED_ESCALATION_DWELL_SECONDS)
            )
        return dwell_seconds

    def _update_sustained_state(self, current_total_power: float) -> bool:
        """
        Track how long the load has stayed at or above the effective budget.

        Arms a monotonic timer when the load reaches the effective budget and
        disarms it when the load drops below. After a sustained shed the dwell
        time shortens to the escalation dwell while the load keeps sitting at
        or above the effective budget, so consecutive rungs of the appliance
        ladder are shed at a faster cadence when one shed was not enough.

        Args:
            current_total_power: Latest total power reading in watts.

        Returns:
            True when the sustained-load dwell time has elapsed and an
            appliance should be shed.

        """
        if not self._sustained_enabled or self._power_budget <= 0:
            self._reset_sustained_tracking()
            return False

        main_sensor_state = self.hass.states.get(self._main_power_sensor_entity_id)
        if main_sensor_state is None or main_sensor_state.state in (
            "unknown",
            "unavailable",
        ):
            if self._sustained_since is not None:
                _LOGGER.debug(
                    "Sustained-load tracking disarmed: main power sensor %s "
                    "is unavailable, last reading is stale",
                    self._main_power_sensor_entity_id,
                )
            self._reset_sustained_tracking()
            return False

        if current_total_power < self.effective_budget:
            if self._sustained_since is None:
                self._below_arm_since = None
                return False
            now = monotonic()
            if self._below_arm_since is None:
                self._below_arm_since = now
                _LOGGER.debug(
                    "Sustained-load dip: load %s W below effective budget "
                    "%s W; disarming in %s s unless it recovers",
                    current_total_power,
                    round(self.effective_budget),
                    SUSTAINED_DISARM_GRACE_SECONDS,
                )
            if now - self._below_arm_since >= SUSTAINED_DISARM_GRACE_SECONDS:
                _LOGGER.debug(
                    "Sustained-load tracking disarmed: load stayed below the "
                    "effective budget of %s W for %s s",
                    round(self.effective_budget),
                    SUSTAINED_DISARM_GRACE_SECONDS,
                )
                self._reset_sustained_tracking()
            else:
                self._schedule_sustained_check(
                    SUSTAINED_DISARM_GRACE_SECONDS - (now - self._below_arm_since)
                )
            return False

        now = monotonic()
        if (
            self._below_arm_since is not None
            and now - self._below_arm_since >= SUSTAINED_DISARM_GRACE_SECONDS
        ):
            _LOGGER.debug(
                "Sustained-load tracking disarmed retroactively: the load was "
                "below the effective budget for over %s s during a gap with "
                "no sensor updates; starting a fresh episode",
                SUSTAINED_DISARM_GRACE_SECONDS,
            )
            self._reset_sustained_tracking()
        self._below_arm_since = None
        if self._sustained_since is None:
            self._sustained_since = now
            _LOGGER.debug(
                "Sustained-load tracking armed: load %s W at or above "
                "effective budget %s W",
                current_total_power,
                round(self.effective_budget),
            )

        dwell_seconds = self._current_dwell_seconds(now)
        elapsed = now - self._sustained_since
        if elapsed < dwell_seconds:
            if self._sustained_check_unsub is None:
                self._schedule_sustained_check(dwell_seconds - elapsed)
            return False
        return True

    def get_diagnostics_snapshot(self) -> dict[str, Any]:
        """Return runtime diagnostics data for troubleshooting."""
        now = monotonic()
        return {
            "entry_id": self.entry.entry_id,
            "power_budget_watt": self._power_budget,
            "effective_budget_watt": self.effective_budget,
            "main_power_sensor_entity_id": self._main_power_sensor_entity_id,
            "monitored_sensor_count": len(self._monitored_sensors),
            "monitored_sensors": self._monitored_sensors,
            "is_over_budget": self._was_over_budget,
            "sustained_shedding": {
                "enabled": self._sustained_enabled,
                "threshold_percent": self._sustained_threshold_percent,
                "duration_seconds": self._sustained_duration_seconds,
                "armed": self._sustained_since is not None,
                "armed_for_seconds": (
                    round(now - self._sustained_since)
                    if self._sustained_since is not None
                    else None
                ),
                "below_arm_for_seconds": (
                    round(now - self._below_arm_since)
                    if self._below_arm_since is not None
                    else None
                ),
                "last_shed_seconds_ago": (
                    round(now - self._last_sustained_shed_at)
                    if self._last_sustained_shed_at is not None
                    else None
                ),
                "check_timer_scheduled": self._sustained_check_unsub is not None,
            },
            "listener_status": {
                "main_power_sensor_listener": self._main_power_sensor_unsub is not None,
                "monitored_sensors_listener": self._monitored_sensors_unsub is not None,
                "appliance_listener": self._appliance_unsub is not None,
            },
            "availability": {
                "currently_unavailable": dict(self._unavailable_entities),
                "recent_events": list(self._availability_events),
            },
            "notifications": self._event_dispatcher.get_diagnostics_snapshot(),
            "power_monitor": self._power_monitor.get_diagnostics_snapshot(),
            "appliance_controller": (
                self._appliance_controller.get_diagnostics_snapshot()
            ),
        }

    @callback
    def _balance_up(self) -> None:
        """Turn on appliances that can safely fit within power budget."""
        callbacks = BalancingCallbacks(
            get_total_power=self.get_total_house_power,
            get_expected_power_restoration=(
                self._appliance_controller.get_expected_power_restoration
            ),
            get_sensor_power_for_appliance=self._get_sensor_power_for_appliance,
            cancel_scheduled_turn_on=(
                self._appliance_controller.cancel_scheduled_turn_on
            ),
            reduce_estimated_power=self._power_monitor.reduce_estimated_power,
            is_appliance_balanced_off=(
                self._appliance_controller.is_appliance_balanced_off
            ),
            is_in_cooldown=self._appliance_controller.is_in_cooldown,
            is_appliance_shed=self._appliance_controller.is_appliance_shed,
        )

        self._balancing_engine.balance_up(
            callbacks,
            self._appliance_controller.get_balanced_off_appliances(),
            self._restore_appliance,
        )

    async def _restore_appliance(self, entity_id: str, reason: str) -> None:
        """
        Restore an appliance and remove it from balanced off tracking.

        Args:
            entity_id: Entity ID of the appliance to restore.
            reason: Reason for restoring the appliance.

        """
        await self._appliance_controller.turn_on_appliance_service(entity_id, reason)
        self._appliance_controller.remove_from_balanced_off(entity_id)

    @callback
    def _balance_down(self, reason: str | None = None) -> bool:
        """Turn off appliances to bring power usage below budget."""
        return self._balancing_engine.balance_down(
            self._appliance_controller.is_appliance_shed,
            self._get_sensor_power_for_appliance,
            self._power_monitor.reduce_estimated_power,
            self._appliance_controller.is_appliance_balanced_off,
            self._turn_off_appliance_for_balancing,
            reason,
        )

    async def _turn_off_appliance_for_balancing(
        self, entity_id: str, reason: str
    ) -> None:
        """
        Turn off an appliance for balancing and schedule auto turn on.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.

        """
        expected_power = self._get_sensor_power_for_appliance(entity_id)
        self._appliance_controller.mark_appliance_balanced_off(entity_id, reason)
        try:
            await self._appliance_controller.turn_off_appliance_service(
                entity_id, reason
            )
        except Exception:
            appliance_state = self.hass.states.get(entity_id)
            if appliance_state is not None and appliance_state.state == "off":
                _LOGGER.warning(
                    "Turn-off service call for %s failed but the appliance "
                    "is off; keeping balanced-off tracking",
                    entity_id,
                )
            else:
                self._appliance_controller.remove_from_balanced_off(entity_id)
                raise

        self._appliance_controller.schedule_auto_turn_on(
            entity_id,
            expected_power,
            self.get_total_house_power,
            self._get_effective_budget,
        )

    def _get_effective_budget(self) -> float:
        """Return the current effective budget for restore headroom checks."""
        return self.effective_budget

    def _get_sensor_power_for_appliance(self, appliance_entity_id: str) -> float:
        """Get the current power consumption for an appliance's sensor."""
        sensor_id = self._appliance_controller.get_sensor_for_appliance(
            appliance_entity_id
        )
        if sensor_id:
            measured = self._power_monitor.get_sensor_power(sensor_id)
            if measured > 0:
                return measured
        return self._appliance_controller.get_nominal_power(appliance_entity_id)

    def manages_entity(self, entity_id: str) -> bool:
        """Check if this PowerLoadBalancer instance manages the given entity."""
        if entity_id == self._main_power_sensor_entity_id:
            return True

        monitored_entities = [s.get(CONF_ENTITY_ID) for s in self._monitored_sensors]
        if entity_id in monitored_entities:
            return True

        appliance_entities = [s.get(CONF_APPLIANCE) for s in self._monitored_sensors]
        return entity_id in appliance_entities

    async def async_turn_off_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> None:
        """
        Handle the turn_off_appliance service call.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.
            context: Optional Home Assistant context.

        """
        await self._appliance_controller.turn_off_appliance_service(
            entity_id, reason, context
        )

    async def async_turn_on_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> None:
        """
        Handle the turn_on_appliance service call.

        Args:
            entity_id: Entity ID of the appliance to turn on.
            reason: Reason for turning on the appliance.
            context: Optional Home Assistant context.

        """
        await self._appliance_controller.turn_on_appliance_service(
            entity_id, reason, context
        )
