"""
Entities that belong to one appliance rather than to the balancer.

Each appliance subentry gets a balancing switch, a learned-draw sensor and a
shed indicator. They are attached to the appliance's own device -- the water
heater's, the smart plug's -- so they show on the page where somebody already
looks at that appliance, the way Home Assistant's own helpers attach to the
device they wrap: by setting `device_entry`, which links the entity without
adding this integration to that device. An appliance with no device of its
own (a template switch, say) still gets the entities, named after it.

A device that is removed and added back takes its old id, but every entity of
another integration on it loses its link on the way (the entity registry sets
`device_id` to None on removal). Some integrations do exactly that while
starting -- the generic water heater rebuilds its device -- so each entity
watches its appliance and power sensor, and the device registry, and links
itself again once the device is back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import Event, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.device_registry import EVENT_DEVICE_REGISTRY_UPDATED
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_entity_registry_updated_event

from .const import CONF_APPLIANCE, SUBENTRY_APPLIANCE

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry, ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry
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
        sensor = subentry.data.get(CONF_ENTITY_ID)
        self._sensor: str | None = sensor if isinstance(sensor, str) else None
        device = self._resolve_device(hass)
        if device is not None:
            self.device_entry = device
            self._attr_has_entity_name = True
        else:
            self._attr_has_entity_name = False
            self._attr_name = f"{subentry.title} {self.fallback_label}"

    def _resolve_device(self, hass: HomeAssistant) -> DeviceEntry | None:
        """Return the appliance's device, else its power sensor's, else None."""
        device = async_entity_id_to_device(hass, self._appliance)
        if device is None and self._sensor:
            device = async_entity_id_to_device(hass, self._sensor)
        return device

    async def async_added_to_hass(self) -> None:
        """Follow the balancer's changes for this appliance, and its device."""
        await super().async_added_to_hass()
        self.async_on_remove(self._balancer.add_appliance_listener(self._refresh))
        if not self._attr_has_entity_name:
            return
        watched = [self._appliance, *([self._sensor] if self._sensor else [])]
        self.async_on_remove(
            async_track_entity_registry_updated_event(
                self.hass, watched, self._async_relink
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_DEVICE_REGISTRY_UPDATED, self._async_relink
            )
        )
        self._async_relink()

    @callback
    def _async_relink(self, _event: Event | None = None) -> None:
        """
        Link the entity to its appliance's device again if it lost the link.

        Only for an entity created on a device: one named after its appliance
        keeps that name until the next reload.
        """
        entry = self.registry_entry
        if entry is None:
            return
        device = self._resolve_device(self.hass)
        if device is None or entry.device_id == device.id:
            return
        er.async_get(self.hass).async_update_entity(self.entity_id, device_id=device.id)

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
