"""Binary sensor platform: whether the balancer is holding an appliance off."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorEntity

from .appliance_entities import ApplianceEntity, add_appliance_entities
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .power_balancer import PowerLoadBalancer


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one shed indicator per appliance."""
    power_balancer: PowerLoadBalancer = hass.data[DOMAIN][config_entry.entry_id]
    add_appliance_entities(
        config_entry,
        async_add_entities,
        lambda subentry: [ApplianceShedSensor(hass, power_balancer, subentry)],
    )


class ApplianceShedSensor(ApplianceEntity, BinarySensorEntity):
    """
    On while the balancer is holding this appliance off.

    Says why and since when, beside the appliance itself, so a cold tank's
    device page answers "is it the balancer?" without opening the log.
    """

    entity_key = "shed_by_balancer"
    fallback_label = "shed by load balancer"
    _attr_icon = "mdi:power-plug-off-outline"

    @property
    def is_on(self) -> bool:
        """Return True while the balancer holds the appliance off."""
        return self._balancer.shed_info(self._appliance) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why the appliance was shed and when."""
        info = self._balancer.shed_info(self._appliance) or {}
        return {
            "reason": info.get("reason"),
            "shed_at": info.get("shed_at"),
            "balanced": self._balancer.is_balanced(self._appliance),
        }
