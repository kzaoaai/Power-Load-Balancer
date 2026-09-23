"""
Telling a deliberate choice apart from a fault.

`hass.states.get()` returns nothing for three entirely different situations:
an entity switched off for the season, an entity id that was mistyped into the
options and never existed, and an entity that is genuinely down right now.
They look identical from the state machine and they deserve opposite handling
-- only the last one is anybody's problem -- so the answer has to come from
the entity registry, which is the only place that records intent.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

UNUSABLE_STATES = ("unknown", "unavailable")


class EntityHealth(Enum):
    """Why an entity is or is not usable for balancing."""

    OK = "ok"
    """Reporting a usable state."""

    DISABLED = "disabled"
    """Registered but switched off by a person, a device or its config entry.

    A deliberate choice. Never a fault, never worth a repair notice.
    """

    MISSING = "missing"
    """Not in the entity registry at all.

    Nothing is broken out in the house: the configuration names something that
    does not exist here. Only editing the options can fix it, so it will never
    clear on its own and reporting it as an outage is misleading.
    """

    AWAITING_DISABLED_ENTRY = "awaiting_disabled_entry"
    """Deleted along with its config entry, which is still here and disabled.

    Indistinguishable from MISSING by entity id alone, and the opposite
    situation: re-enabling the entry makes the integration recreate the
    entity from its unique id. Calling this a misconfiguration would tell
    somebody their seasonal shutdown is a permanent mistake.
    """

    UNAVAILABLE = "unavailable"
    """Registered, enabled, and not reporting a usable state.

    The only one of these that is an actual fault.
    """


def classify_entity(
    hass: HomeAssistant, entity_id: str
) -> tuple[EntityHealth, str | None]:
    """
    Return why an entity is or is not usable, and the state it reported.

    Order matters: a usable state settles it before the registry is consulted,
    so an entity written straight into the state machine by a template or a
    script still counts as working even though it was never registered.
    """
    state = hass.states.get(entity_id)
    state_value = state.state if state is not None else None

    if state is not None:
        # A state object at all means something here is publishing this
        # entity, registry or not -- templates and scripts write straight to
        # the state machine. So an unusable value is an outage, never proof
        # that the entity does not exist.
        if state_value not in UNUSABLE_STATES:
            return EntityHealth.OK, state_value
        return EntityHealth.UNAVAILABLE, state_value

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is not None:
        if entry.disabled_by is not None:
            return EntityHealth.DISABLED, state_value
        return EntityHealth.UNAVAILABLE, state_value

    if _has_disabled_owner(hass, registry, entity_id):
        return EntityHealth.AWAITING_DISABLED_ENTRY, state_value
    return EntityHealth.MISSING, state_value


def _has_disabled_owner(
    hass: HomeAssistant, registry: er.EntityRegistry, entity_id: str
) -> bool:
    """
    Return True if a disabled config entry would recreate this entity.

    Disabling an integration does not treat all of its entities alike: some
    are kept registered and marked disabled, others are removed outright and
    left only as a deletion record. Both come back when the entry is enabled
    again, so both are somebody's seasonal decision rather than a fault --
    but only the first is visible from the live registry, which is why the
    deletion records have to be consulted too.
    """
    deleted = next(
        (
            record
            for record in registry.deleted_entities.values()
            if record.entity_id == entity_id
        ),
        None,
    )
    if deleted is None or not deleted.config_entry_id:
        return False

    owner = hass.config_entries.async_get_entry(deleted.config_entry_id)
    # An entry that is gone will never recreate anything; an entry that is
    # enabled has had its chance and still has not, so neither is seasonal.
    return owner is not None and owner.disabled_by is not None
