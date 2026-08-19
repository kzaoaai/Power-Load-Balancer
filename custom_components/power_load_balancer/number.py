"""Number platform for the Power Load Balancer integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_POWER_BUDGET_WATT, DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .power_balancer import PowerLoadBalancer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Power Load Balancer number entity from a config entry."""
    _LOGGER.debug("Setting up power_load_balancer number platform")

    power_balancer: PowerLoadBalancer = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([PowerBudgetNumber(power_balancer, config_entry)])


class PowerBudgetNumber(NumberEntity):
    """Number entity to adjust the power budget at runtime."""

    _attr_has_entity_name = True
    _attr_name = "Power Budget"
    _attr_icon = "mdi:flash"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 20000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, balancer: PowerLoadBalancer, config_entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        self._balancer = balancer
        self._config_entry = config_entry
        self._attr_unique_id = f"{self._balancer.entry.entry_id}_power_budget"

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information for the number entity."""
        if self._balancer.entry.entry_id:
            return DeviceInfo(
                identifiers={(DOMAIN, self._balancer.entry.entry_id)},
                name="Power Load Balancer",
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL,
            )
        return None

    @property
    def native_value(self) -> float:
        """Return the current power budget."""
        return self._balancer.power_budget

    async def async_set_native_value(self, value: float) -> None:
        """Set the power budget value."""
        new_budget = int(value)
        self._balancer.set_power_budget(new_budget)

        # Set skip_reload so the update listener doesn't trigger a full reload
        self._balancer.skip_reload = True

        # Persist to config entry so the value survives restarts
        new_data = {**self._config_entry.data, CONF_POWER_BUDGET_WATT: new_budget}
        new_options = {
            **self._config_entry.options,
            CONF_POWER_BUDGET_WATT: new_budget,
        }
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data=new_data,
            options=new_options,
        )

        _LOGGER.info("Power budget updated to %s W", new_budget)
        self.async_write_ha_state()
