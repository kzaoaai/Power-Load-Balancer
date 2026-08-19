"""Switch platform for the Power Load Balancer integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN

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
    """Set up the Power Load Balancer switch from a config entry."""
    _LOGGER.debug("Setting up power_load_balancer switch platform")

    power_balancer: PowerLoadBalancer = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([PowerLoadBalancerEnabledSwitch(power_balancer)])


class PowerLoadBalancerEnabledSwitch(SwitchEntity, RestoreEntity):
    """Switch entity to enable/disable Power Load Balancer."""

    _attr_has_entity_name = True
    _attr_translation_key = "enabled"
    _attr_icon = "mdi:power"

    def __init__(self, balancer: PowerLoadBalancer) -> None:
        """Initialize the switch."""
        self._balancer = balancer
        self._attr_unique_id = f"{self._balancer.entry.entry_id}_enabled"

    async def async_added_to_hass(self) -> None:
        """Restore the previous enabled state across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == "off":
            self._balancer.set_enabled(False)
            self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information for the switch."""
        if self._balancer.entry.entry_id:
            return DeviceInfo(
                identifiers={(DOMAIN, self._balancer.entry.entry_id)},
                name="Power Load Balancer",
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL,
            )
        return None

    @property
    def is_on(self) -> bool:
        """Return True if the balancer is enabled."""
        return self._balancer.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable power load balancing."""
        self._balancer.set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable power load balancing and restore all balanced-off appliances."""
        await self._balancer.async_disable_and_restore()
        self.async_write_ha_state()
