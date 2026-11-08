"""
Core balancing logic for the Power Load Balancer integration.

This module contains the PowerLoadBalancer class which manages power monitoring,
appliance control, and automatic load balancing based on a configured power budget.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import Context, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
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
    CONF_POWER_BUDGET_WATT,
    CONF_POWER_SENSORS,
    CONF_SUSTAINED_DURATION_SECONDS,
    CONF_SUSTAINED_ENABLED,
    CONF_SUSTAINED_THRESHOLD_PERCENT,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_SUSTAINED_DURATION_SECONDS,
    DEFAULT_SUSTAINED_ENABLED,
    DEFAULT_SUSTAINED_THRESHOLD_PERCENT,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    ENTITY_HEALTH_SWEEP_SECONDS,
    ISSUE_TRANSLATION_KEY_DEVICE_NOT_CONTROLLABLE,
    ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE,
    ISSUE_TRANSLATION_KEY_ENTITY_NOT_REGISTERED,
    NON_BINARY_ACTIVE_STATE_DOMAINS,
    NOTIFY_BUDGET,
    NOTIFY_PROBLEM,
    POWER_TICK_SWEEP_SECONDS,
    SHED_STORE_VERSION,
    SHED_VERIFY_GRACE_SECONDS,
    SUSTAINED_DISARM_GRACE_SECONDS,
    SUSTAINED_ESCALATION_DWELL_SECONDS,
    SUSTAINED_ESCALATION_WINDOW_SECONDS,
    UNAVAILABLE_DEBOUNCE_SECONDS,
    appliance_control_issue_id,
    shed_store_key,
)
from .context_logger import ContextLogger
from .entity_health import EntityHealth, classify_entity
from .event_dispatcher import BalancerEventDispatcher
from .exceptions import ConfigurationError
from .notify_channels import (
    resolve_notify_channels,
    resolve_notify_services,
)
from .power_monitor import PowerMonitor

_LOGGER = logging.getLogger(__name__)
AVAILABILITY_EVENT_HISTORY_SIZE = 100
UNAVAILABLE_ENTITY_ISSUE_PREFIX = "balancing_entity_unavailable"
NOT_REGISTERED_ENTITY_ISSUE_PREFIX = "balancing_entity_not_registered"
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
    _not_registered_entities: set[str]
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
        self._not_registered_entities = set()
        self._health_sweep_unsub: Callable[[], None] | None = None
        self._health_started_unsub: Callable[[], None] | None = None
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
        self._last_tick_sweep: float | None = None
        self._pending_sensor: Any = None
        self._event_dispatcher = BalancerEventDispatcher(
            hass,
            config_data["entry_id"],
            notify_services=resolve_notify_services(config_data),
            channels=resolve_notify_channels(config_data),
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

        self._initialize_controllability_tracking()
        store: Store[dict[str, Any]] = Store(
            self.hass,
            SHED_STORE_VERSION,
            shed_store_key(self._config_data["entry_id"]),
        )
        try:
            saved = await store.async_load() or {}
        except Exception:
            _LOGGER.exception("Could not read the saved shed record; ignoring it")
            saved = {}
        self._appliance_controller.attach_store(store)
        self._adopt_appliances_already_shed()
        appliances = saved.get("appliances") if isinstance(saved, dict) else None
        self._adopt_saved_sheds(appliances if isinstance(appliances, dict) else {})

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

    def _adopt_saved_sheds(self, saved: dict[str, Any]) -> None:
        """
        Take back appliances the saved record says the balancer had shed.

        Plain "off" does not say who turned an appliance off, so the record is
        the only evidence. An appliance that is running again was taken back by
        someone and is left alone; one that is still off, or not loaded yet,
        is held off and restored when there is headroom.
        """
        configured = {s.get(CONF_APPLIANCE) for s in self._monitored_sensors}
        for entity_id, record in saved.items():
            if entity_id not in configured or not isinstance(record, dict):
                continue
            controller = self._appliance_controller
            if controller.is_appliance_balanced_off(entity_id):
                controller.adopt_saved_record(entity_id, record, mark=False)
                continue
            if self._state_is_drawing(entity_id, self.hass.states.get(entity_id)):
                continue
            controller.adopt_saved_record(entity_id, record)
            _LOGGER.info(
                "Adopted %s from the saved shed record; it will be restored "
                "when there is headroom",
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

        self._start_health_tracking()

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

    def _tracked_entities(self) -> list[tuple[str, str]]:
        """Return every entity balancing depends on, with its role."""
        tracked: list[tuple[str, str]] = [
            (self._main_power_sensor_entity_id, "main_power_sensor")
        ]
        tracked.extend(
            (sensor_config[CONF_ENTITY_ID], "power_sensor")
            for sensor_config in self._monitored_sensors
        )
        tracked.extend(
            (sensor_config[CONF_APPLIANCE], "appliance")
            for sensor_config in self._monitored_sensors
        )
        return tracked

    @callback
    def _evaluate_entity_health(self, *_args: Any) -> None:
        """
        Re-judge every tracked entity and bring its repair notices into line.

        Deliberately a sweep over current state rather than a reaction to
        state edges. A dead entity sends no event to announce that it is still
        dead, and an entity that recovered while listeners were still being
        registered would never send the event that cleared it -- which is how
        a repair for a healthy sensor stayed open for a day. Judging the
        present state every time makes both cases self-correcting.
        """
        for entity_id, entity_type in self._tracked_entities():
            if not isinstance(entity_id, str) or not entity_id:
                continue
            health, state_value = classify_entity(self.hass, entity_id)
            self._apply_entity_health(entity_id, entity_type, health, state_value)

    def _apply_entity_health(
        self,
        entity_id: str,
        entity_type: str,
        health: EntityHealth,
        state_value: str | None,
    ) -> None:
        """Raise, keep or withdraw the repair notices for one entity."""
        if health is EntityHealth.MISSING:
            self._clear_unavailable_issue(entity_id, entity_type, state_value)
            self._report_not_registered(entity_id, entity_type)
            return

        self._clear_not_registered_issue(entity_id)

        if health in (
            EntityHealth.OK,
            EntityHealth.DISABLED,
            EntityHealth.AWAITING_DISABLED_ENTRY,
        ):
            # Both disabled shapes are somebody's decision -- seasonal heaters
            # get switched off every summer here -- and both come back when
            # the entry is re-enabled, so neither is raised as a fault.
            self._clear_unavailable_issue(entity_id, entity_type, state_value)
            return

        self._report_unavailable(entity_id, entity_type, state_value)

    def _report_unavailable(
        self, entity_id: str, entity_type: str, state_value: str | None
    ) -> None:
        """Track an unavailable entity, raising a repair once it persists."""
        now_iso = dt.utcnow().isoformat(timespec="seconds")
        tracked = self._unavailable_entities.get(entity_id)

        if tracked is None:
            tracked = {
                "entity": entity_id,
                "entity_type": entity_type,
                "state": state_value,
                "first_seen": now_iso,
                "last_seen": now_iso,
                "since": monotonic(),
                "reported": False,
            }
            self._unavailable_entities[entity_id] = tracked
            self._record_availability_event(
                {
                    "timestamp": now_iso,
                    "event": "became_unavailable",
                    "entity": entity_id,
                    "entity_type": entity_type,
                    "state": state_value,
                    "reason": "entity unavailable for balancing",
                }
            )
        else:
            tracked["last_seen"] = now_iso
            tracked["state"] = state_value

        if tracked.get("reported"):
            return
        if monotonic() - float(tracked["since"]) < UNAVAILABLE_DEBOUNCE_SECONDS:
            return

        tracked["reported"] = True
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
            "Entity %s has been unavailable for balancing for over %s s "
            "(type=%s, state=%s)",
            entity_id,
            UNAVAILABLE_DEBOUNCE_SECONDS,
            entity_type,
            state_value,
        )

    def _clear_unavailable_issue(
        self, entity_id: str, entity_type: str, state_value: str | None
    ) -> None:
        """Withdraw an unavailability notice once the entity is usable again."""
        # The appliance controller raises its own notice when a service call
        # finds the appliance missing, under an id nothing else cleared; a
        # usable entity withdraws it here too.
        ir.async_delete_issue(self.hass, DOMAIN, appliance_control_issue_id(entity_id))
        tracked = self._unavailable_entities.pop(entity_id, None)
        if tracked is None:
            return

        self._record_availability_event(
            {
                "timestamp": dt.utcnow().isoformat(timespec="seconds"),
                "event": "restored",
                "entity": entity_id,
                "entity_type": entity_type,
                "state": state_value,
                "unavailable_since": tracked.get("first_seen"),
                "reason": "entity restored for balancing",
            }
        )
        ir.async_delete_issue(
            self.hass, DOMAIN, self._get_unavailable_issue_id(entity_id)
        )
        if tracked.get("reported"):
            _LOGGER.info(
                "Entity %s is available again for balancing (type=%s, state=%s)",
                entity_id,
                entity_type,
                state_value,
            )

    def _report_not_registered(self, entity_id: str, entity_type: str) -> None:
        """Flag an entity that the configuration names but that does not exist."""
        if entity_id in self._not_registered_entities:
            return
        self._not_registered_entities.add(entity_id)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._get_not_registered_issue_id(entity_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TRANSLATION_KEY_ENTITY_NOT_REGISTERED,
            translation_placeholders={"entity_id": entity_id},
        )
        _LOGGER.warning(
            "Entity %s is configured for balancing (type=%s) but is not in the "
            "entity registry; balancing cannot use it until the configuration "
            "names an entity that exists",
            entity_id,
            entity_type,
        )

    def _clear_not_registered_issue(self, entity_id: str) -> None:
        """Withdraw a not-registered notice once the entity exists."""
        if entity_id not in self._not_registered_entities:
            return
        self._not_registered_entities.discard(entity_id)
        ir.async_delete_issue(
            self.hass, DOMAIN, self._get_not_registered_issue_id(entity_id)
        )

    def _get_not_registered_issue_id(self, entity_id: str) -> str:
        """Return a stable issue ID for an entity that does not exist."""
        sanitized_entity = entity_id.replace(".", "_")
        return (
            f"{NOT_REGISTERED_ENTITY_ISSUE_PREFIX}_{self.entry.entry_id}_"
            f"{sanitized_entity}"
        )

    def _start_health_tracking(self) -> None:
        """
        Begin judging entity availability, once Home Assistant is up.

        Nothing is judged during startup: every entity is briefly unavailable
        then, and integrations come back in their own time, so evaluating
        early manufactures a repair for each balanced entity that happens to
        be slow. The sweep then keeps running, because an entity that stays
        down never sends another event.
        """

        @callback
        def _begin(_event: Any = None) -> None:
            self._evaluate_entity_health()
            self._health_sweep_unsub = async_track_time_interval(
                self.hass,
                self._evaluate_entity_health,
                timedelta(seconds=ENTITY_HEALTH_SWEEP_SECONDS),
            )

        self._health_started_unsub = async_at_started(self.hass, _begin)

    def _clear_unavailable_entity_issues(self) -> None:
        """Delete all outstanding Repairs issues this balancer raised."""
        for entity_id in list(self._unavailable_entities):
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                self._get_unavailable_issue_id(entity_id),
            )
        for entity_id in list(self._not_registered_entities):
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                self._get_not_registered_issue_id(entity_id),
            )
        self._not_registered_entities.clear()

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
            await self._appliance_controller.async_flush_store()
            self._appliance_controller.cleanup()
            self._clear_unavailable_entity_issues()
            self._clear_non_controllable_entity_issues()
            self._unavailable_entities.clear()
            self._not_registered_entities.clear()
            self._availability_events.clear()
            for unsub in (self._health_sweep_unsub, self._health_started_unsub):
                if unsub is not None:
                    unsub()
            self._health_sweep_unsub = None
            self._health_started_unsub = None
            self._non_controllable_media_players.clear()
            self._event_log_sensor = None
            self._pending_sensor = None

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
        self._unverified_sheds.clear()
        self._refresh_pending_sensor()
        _LOGGER.info("Power Load Balancer %s", "enabled" if enabled else "disabled")

    def _reset_sustained_tracking(self) -> None:
        """Fully reset the sustained-load tracking state."""
        self._sustained_since = None
        self._last_sustained_shed_at = None
        self._reported_sustained_failure = False
        self._below_arm_since = None
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
            f"(effective {round(self.effective_budget)} W)",
            category=NOTIFY_BUDGET,
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
        self._refresh_pending_sensor()
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

    def register_pending_sensor(self, sensor: Any) -> None:
        """Register the sensor that reports queued actions."""
        self._pending_sensor = sensor

    async def _handle_power_sensor_state_change(self, event: Any) -> None:
        """
        Handle state changes for power sensors triggered by Home Assistant events.

        Args:
            event: The state change event from Home Assistant.

        """
        # Which entity moved does not matter: the sweep re-judges all of them,
        # which is what makes a recovery nobody reported still get noticed.
        # A meter ticking along between two valid readings changes nothing
        # the sweep judges, so those ticks run it at most every
        # POWER_TICK_SWEEP_SECONDS; any reading that is, or was, invalid
        # runs it at once.
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        invalid = ("unknown", "unavailable")
        routine_tick = (
            old_state is not None
            and new_state is not None
            and old_state.state not in invalid
            and new_state.state not in invalid
        )
        now = monotonic()
        if (
            not routine_tick
            or self._last_tick_sweep is None
            or now - self._last_tick_sweep >= POWER_TICK_SWEEP_SECONDS
        ):
            self._last_tick_sweep = now
            self._evaluate_entity_health()

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
            self._evaluate_entity_health()
            _LOGGER.debug("Ignoring invalid state for appliance %s", entity_id)
            return

        if isinstance(entity_id, str):
            self._evaluate_entity_health()
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
            # An appliance coming back from unavailable, or appearing at
            # startup, was not switched on by anyone: it was running all along
            # and its draw is already in the meter total. Vetoing it would
            # count that draw twice and switch off a running appliance.
            reappeared = old_state is None or old_state.state in (
                "unknown",
                "unavailable",
            )
            await self._handle_appliance_turn_on(entity_id, veto=not reappeared)
        elif not is_now_active and was_active:
            self._handle_appliance_turn_off(entity_id)

        self.async_check_and_balance()

    async def _handle_appliance_turn_on(
        self, entity_id: str, *, veto: bool = True
    ) -> None:
        """
        Handle an appliance being turned on.

        The veto below is for people: it sheds an appliance someone switched
        on into a load it does not fit. The balancer's own restores arrive
        here too, and they were already sized against the headroom when they
        were chosen, so they skip it, as does an appliance that only
        reappeared after being unavailable.
        """
        own_restore = self._appliance_controller.consume_own_restore(entity_id)
        self._appliance_controller.remove_from_balanced_off(entity_id)
        self._appliance_controller.cancel_scheduled_turn_on(entity_id)

        if not self._enabled:
            return

        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == entity_id:
                power_to_add = self._power_monitor.calculate_sensor_power(
                    sensor_config
                ) or self._appliance_controller.get_nominal_power(entity_id)

                if (
                    veto
                    and not own_restore
                    and self._power_monitor.would_exceed_budget(power_to_add)
                ):
                    await self._appliance_controller.turn_off_appliance(
                        entity_id,
                        reason=(
                            "turn-on vetoed: load "
                            f"{round(self.get_total_house_power())} W plus "
                            f"{round(power_to_add)} W would exceed the "
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
            self._refresh_pending_sensor()
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
                        f"available",
                        category=NOTIFY_PROBLEM,
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
        self._refresh_pending_sensor()

        if (
            self._sustained_enabled
            and self._sustained_since is not None
            and self._sustained_check_unsub is None
        ):
            now = monotonic()
            remaining = self._current_dwell_seconds(now) - (now - self._sustained_since)
            self._schedule_sustained_check(remaining)

    def _refresh_pending_sensor(self) -> None:
        """Let the pending-actions sensor recompute, if it exists yet."""
        if self._pending_sensor is not None:
            self._pending_sensor.refresh()

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
        held = set(self._appliance_controller.get_balanced_off_appliances())
        self._unverified_sheds &= held

        for entity_id in held:
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
                f"after {round(age)} s",
                category=NOTIFY_PROBLEM,
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

    def get_pending_actions(self) -> dict[str, Any]:
        """
        Describe what the balancer is currently waiting to do.

        Times are reported as the moment an action becomes due rather than as
        a countdown. A countdown would change on every evaluation, and Home
        Assistant records a new row whenever a state or any attribute changes,
        so a ticking value would write history every few seconds for the whole
        wait. A due time is stable until the plan itself changes.
        """
        if not self._enabled:
            return {
                "count": 0,
                "summary": "Balancer disabled",
                "next_due_at": None,
                "pending": [],
            }

        pending: list[dict[str, Any]] = []
        now = monotonic()

        if self._sustained_since is not None:
            remaining = self._current_dwell_seconds(now) - (now - self._sustained_since)
            candidate = self._balancing_engine.next_shed_candidate(
                self._appliance_controller.is_appliance_shed,
                self._appliance_controller.is_appliance_balanced_off,
            )
            if candidate is None:
                pending.append(
                    {
                        "action": "attention",
                        "entity_id": None,
                        "due_at": None,
                        "reason": (
                            f"load at or above the effective budget of "
                            f"{round(self.effective_budget)} W and nothing "
                            f"is available to shed"
                        ),
                    }
                )
            else:
                pending.append(
                    {
                        "action": "shed",
                        "entity_id": candidate,
                        "due_at": self._due_at(remaining),
                        "reason": (
                            f"load at or above the effective budget of "
                            f"{round(self.effective_budget)} W"
                        ),
                    }
                )

        for entity_id in self._appliance_controller.get_balanced_off_appliances():
            if entity_id in self._unverified_sheds:
                pending.append(
                    {
                        "action": "attention",
                        "entity_id": entity_id,
                        "due_at": None,
                        "reason": "shed did not take effect; still drawing",
                    }
                )
                continue

            age = self._appliance_controller.get_balanced_off_age(entity_id)
            cooldown = self._appliance_controller.get_cooldown_for_appliance(entity_id)
            remaining = cooldown - age if age is not None else cooldown
            pending.append(
                {
                    "action": "restore",
                    "entity_id": entity_id,
                    "due_at": self._due_at(remaining) if remaining > 0 else None,
                    "reason": (
                        "waiting out the cooldown"
                        if remaining > 0
                        else "waiting for enough headroom to restore"
                    ),
                }
            )

        due_times = [item["due_at"] for item in pending if item["due_at"]]
        return {
            "count": len(pending),
            "summary": self._describe_pending(pending),
            "next_due_at": min(due_times) if due_times else None,
            "pending": pending,
        }

    def _due_at(self, remaining_seconds: float) -> str | None:
        """
        Return when something becomes due, to the nearest second.

        The instant is rounded, never the offset. Rounding the offset and
        truncating the clock quantises the same moment twice, and the two
        quantisations drift against each other as the sub-second phase moves,
        so a deadline that has not changed reports as two alternating
        seconds -- rewriting the entity, and a recorder row, on almost every
        evaluation. That is exactly what reporting an absolute time is meant
        to avoid.
        """
        if remaining_seconds <= 0:
            return None
        due = dt.utcnow() + timedelta(seconds=remaining_seconds)
        rounded = (due + timedelta(microseconds=500000)).replace(microsecond=0)
        return rounded.isoformat()

    def _describe_pending(self, pending: list[dict[str, Any]]) -> str:
        """Summarise pending work without naming times, so it stays stable."""
        if not pending:
            return "Nothing pending"

        parts: list[str] = []
        for action, label in (
            ("shed", "to shed"),
            ("restore", "to restore"),
            ("attention", "needing attention"),
        ):
            names = [
                str(item["entity_id"]).split(".", 1)[-1]
                for item in pending
                if item["action"] == action and item["entity_id"]
            ]
            unnamed = sum(
                1
                for item in pending
                if item["action"] == action and not item["entity_id"]
            )
            if names:
                parts.append(f"{label} {', '.join(names)}")
            elif unnamed:
                parts.append(f"{label} an appliance")
        return "Waiting " + "; ".join(parts) if parts else "Nothing pending"

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
        self._appliance_controller.note_own_restore(entity_id)
        try:
            restored = await self._appliance_controller.turn_on_appliance_service(
                entity_id, reason
            )
        except BaseException:
            self._appliance_controller.forget_own_restore(entity_id)
            raise
        if not restored:
            # Nothing could turn it back on (a climate with no usable HVAC
            # mode, say). Forgetting it would leave it off for good; keep
            # holding it and try again after another cooldown.
            self._appliance_controller.forget_own_restore(entity_id)
            self._appliance_controller.restart_cooldown(entity_id)
            return
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
            if self._appliance_controller.has_stopped(entity_id):
                _LOGGER.warning(
                    "Turn-off service call for %s failed but the appliance "
                    "has stopped; keeping balanced-off tracking",
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
