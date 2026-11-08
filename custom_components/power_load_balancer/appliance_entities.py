"""
Entities that belong to one appliance rather than to the balancer.

Each appliance subentry gets a balancing switch, a learned-draw sensor and a
shed indicator. They are attached to the appliance's own device -- the water
heater's, the smart plug's -- so they show on the page where somebody already
looks at that appliance, the way Home Assistant's own helpers attach to the
device they wrap: by setting `device_entry`, which links the entity without
adding this integration to that device. An appliance with no device of its
own (a template switch, say) still gets the entities, named after it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import callback
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.entity import Entity

from .const import CONF_APPLIANCE, SUBENTRY_APPLIANCE

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry, ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .power_balancer import PowerLoadBalancer


class ApplianceEntity(Entity):
    """Base for an entity that reports on, or controls, one appliance."""

    _attr_should_poll = False
    entity_key: str = ""
    fallback_label: str = ""

    def __init__(
        self,
        hass: HomeAssistant,
        balancer: PowerLoadBalancer,
        subentry: ConfigSubentry,
    ) -> None:
        """Link the entity to its appliance, and to the appliance's device."""
        self._balancer = balancer
        self._subentry_id = subentry.subentry_id
        self._appliance: str = str(subentry.data.get(CONF_APPLIANCE, ""))
        self._attr_unique_id = (
            f"{balancer.entry.entry_id}_{subentry.subentry_id}_{self.entity_key}"
        )
        self._attr_translation_key = self.entity_key
        device = async_entity_id_to_device(hass, self._appliance)
        sensor = subentry.data.get(CONF_ENTITY_ID)
        if device is None and isinstance(sensor, str):
            device = async_entity_id_to_device(hass, sensor)
        if device is not None:
            self.device_entry = device
            self._attr_has_entity_name = True
        else:
            self._attr_has_entity_name = False
            self._attr_name = f"{subentry.title} {self.fallback_label}"

    async def async_added_to_hass(self) -> None:
        """Follow the balancer's changes for this appliance."""
        await super().async_added_to_hass()
        self.async_on_remove(self._balancer.add_appliance_listener(self._refresh))

    @callback
    def _refresh(self) -> None:
        """Write the current state."""
        self.async_write_ha_state()


def add_appliance_entities(
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[ConfigSubentry], list[Entity]],
) -> None:
    """Add a platform's per-appliance entities, each under its subentry."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_APPLIANCE:
            continue
        async_add_entities(factory(subentry), config_subentry_id=subentry.subentry_id)
