"""Sensor platform for the Power Load Balancer integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt

from .appliance_entities import ApplianceEntity, add_appliance_entities
from .const import DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN, LEARN_MIN_SAMPLES

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .power_balancer import PowerLoadBalancer

_LOGGER = logging.getLogger(__name__)

MAX_LOG_SIZE = 50
MAX_LOG_ENTRY_LENGTH = 300
MAX_STATE_LENGTH = 255


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up the Power Load Balancer sensor from a config entry.

    Args:
        hass: Home Assistant instance.
        config_entry: Configuration entry for this integration.
        async_add_entities: Callback to add entities.

    """
    _LOGGER.debug("Setting up power_load_balancer sensor platform")

    power_balancer: PowerLoadBalancer = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities(
        [
            PowerBalancerLogSensor(power_balancer),
            PowerBalancerPendingSensor(power_balancer),
        ]
    )
    add_appliance_entities(
        config_entry,
        async_add_entities,
        lambda subentry: [ApplianceLearnedDrawSensor(hass, power_balancer, subentry)],
    )


class ApplianceLearnedDrawSensor(ApplianceEntity, SensorEntity):
    """
    What the balancer has learned this appliance draws while it runs.

    Unknown until enough readings exist. No state class: it is a slowly
    moving estimate, not a measurement worth long-term statistics.
    """

    entity_key = "learned_draw"
    fallback_label = "PLB learned draw"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:chart-bell-curve"

    @property
    def native_value(self) -> StateType:
        """Return the learned draw, or None while it is still unknown."""
        watts, _samples = self._balancer.learned_draw(self._appliance)
        return watts or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return how many running readings the figure rests on."""
        _watts, samples = self._balancer.learned_draw(self._appliance)
        return {"samples": samples, "samples_needed": LEARN_MIN_SAMPLES}


class PowerBalancerPendingSensor(SensorEntity):
    """
    Reports what the balancer is waiting to do, and when.

    The event log says what already happened; this says what is queued. It
    answers the question a dashboard actually gets asked during an overload:
    is anything about to switch, and which appliance.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "pending_actions"
    _attr_icon = "mdi:timer-sand"
    _attr_native_unit_of_measurement = "actions"

    def __init__(self, balancer: PowerLoadBalancer) -> None:
        """Initialize the pending-actions sensor."""
        self._balancer = balancer
        self._attr_unique_id = f"{balancer.entry.entry_id}_pending_actions"
        self._report: dict[str, Any] = {
            "count": 0,
            "summary": "Nothing pending",
            "next_due_at": None,
            "pending": [],
        }

    async def async_added_to_hass(self) -> None:
        """Register with the balancer and take a first reading."""
        await super().async_added_to_hass()
        self._balancer.register_pending_sensor(self)
        self.refresh()

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information for the sensor."""
        if self._balancer.entry.entry_id:
            return DeviceInfo(
                identifiers={(DOMAIN, self._balancer.entry.entry_id)},
                name="Power Load Balancer",
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL,
            )
        return None

    @property
    def native_value(self) -> StateType:
        """Return how many actions are waiting."""
        return self._report["count"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the queued actions and when each becomes due."""
        return {
            "summary": self._report["summary"],
            "next_due_at": self._report["next_due_at"],
            "pending": self._report["pending"],
        }

    @callback
    def refresh(self) -> None:
        """Recompute the report and publish it if anything changed."""
        report = self._balancer.get_pending_actions()
        if report == self._report:
            return
        self._report = report
        if self.hass is not None:
            self.async_write_ha_state()


class PowerBalancerLogSensor(SensorEntity, RestoreEntity):
    """
    Sensor entity that tracks power balancing events.

    This sensor provides a log of all power balancing actions taken,
    including appliances turned on/off and the reasons for those actions.
    The last state and event history are restored across Home Assistant
    restarts so an incident is still diagnosable after a reboot.
    """

    _attr_should_poll = False
    # The log is kept across restarts by RestoreEntity, not the recorder, and
    # re-recording the whole list on every entry only grows the database.
    _unrecorded_attributes = frozenset({"events"})

    _balancer: PowerLoadBalancer
    _attr_name: str
    _attr_unique_id: str
    _attr_native_value: str
    _attr_extra_state_attributes: dict[str, Any]

    def __init__(self, balancer: PowerLoadBalancer) -> None:
        """
        Initialize the sensor.

        Args:
            balancer: The PowerLoadBalancer instance to track.

        """
        self._balancer = balancer
        self._attr_name = "Power Load Balancer Log"
        self._attr_unique_id = f"{self._balancer.entry.entry_id}_event_log"
        self._attr_native_value = "Initialized"
        self._attr_extra_state_attributes = {"events": []}

    async def async_added_to_hass(self) -> None:
        """Restore prior state and register with the PowerLoadBalancer."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            "unknown",
            "unavailable",
        ):
            self._attr_native_value = last_state.state
            restored_events = last_state.attributes.get("events")
            if isinstance(restored_events, list):
                self._attr_extra_state_attributes["events"] = [
                    str(event) for event in restored_events
                ][-MAX_LOG_SIZE:]
            _LOGGER.debug(
                "Restored %s event log entries from previous session",
                len(self._attr_extra_state_attributes["events"]),
            )

        self._balancer.register_event_log_sensor(self)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information for the sensor."""
        if self._balancer.entry.entry_id:
            return DeviceInfo(
                identifiers={(DOMAIN, self._balancer.entry.entry_id)},
                name="Power Load Balancer",
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL,
            )
        return None

    @callback
    def add_log_entry(self, message: str) -> None:
        """
        Add an entry to the event log.

        Args:
            message: The log message to add.

        """
        timestamp = dt.utcnow().isoformat(timespec="seconds")
        log_entry = f"{timestamp} - {message}"[:MAX_LOG_ENTRY_LENGTH]
        # A new list each time: the current state holds a reference to the
        # old one, and appending in place would rewrite it underneath.
        self._attr_extra_state_attributes["events"] = [
            *self._attr_extra_state_attributes["events"],
            log_entry,
        ][-MAX_LOG_SIZE:]

        # Home Assistant rejects a state over 255 characters and falls back to
        # "unknown" with no attributes, which would drop the whole log.
        self._attr_native_value = message[:MAX_STATE_LENGTH]
        _LOGGER.debug("Power balancer event logged: %s", message)
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        """Return the current state of the sensor."""
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the state attributes containing the event log."""
        return self._attr_extra_state_attributes
