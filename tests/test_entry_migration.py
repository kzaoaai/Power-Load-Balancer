"""
Where an entry's configuration lives, and what the services say when misused.

Before 1.2 the configuration sat in data until the first options save, then in
a full copy in options. Data went stale underneath: a production entry still
carried the very first appliance list there, shadowed only because options
win the merge, and both were rewritten on every budget change.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_load_balancer.const import DOMAIN
from tests.conftest import BUDGET, MAIN_SENSOR, balancer_of, ladder

STALE_DATA = {
    "main_power_sensor": "sensor.old_meter_watts",
    "power_budget_watt": BUDGET,
    "cooldown_seconds": 10,
    "power_sensors": [
        {
            "appliance": "switch.old_heater",
            "entity_id": "sensor.old_heater_power",
            "importance": 1,
            "last_resort": False,
        }
    ],
}

LIVE_OPTIONS = {
    "main_power_sensor": MAIN_SENSOR,
    "power_budget_watt": BUDGET,
    "cooldown_seconds": 120,
    "power_sensors": ladder(),
    "notify_channels": {
        "shed": "service",
        "restore": "none",
        "budget": "none",
        "problem": "both",
    },
    "notify_persistent": False,
    "notify_service": "notify.someone",
}


async def _setup_old_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data=dict(STALE_DATA),
        options=dict(LIVE_OPTIONS),
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_an_old_entry_keeps_one_copy_of_its_live_configuration(hass, world):
    """The stale copy in data is replaced by the one actually in force."""
    entry = await _setup_old_entry(hass)

    assert entry.minor_version == 2
    assert entry.options == {}
    assert entry.data["main_power_sensor"] == MAIN_SENSOR
    assert entry.data["power_sensors"] == ladder()
    assert balancer_of(hass, entry)._main_power_sensor_entity_id == MAIN_SENSOR


async def test_migration_keeps_notifications_in_their_current_shape(hass, world):
    """Legacy keys go, and nothing that was being delivered stops."""
    entry = await _setup_old_entry(hass)

    assert entry.data["notify_channels"] == LIVE_OPTIONS["notify_channels"]
    assert entry.data["notify_services"] == ["notify.someone"]
    for legacy in ("notify_persistent", "notify_service", "notify_categories"):
        assert legacy not in entry.data


async def test_a_budget_change_writes_data_only(hass, world):
    """The budget entity must not recreate a copy in options."""
    entry = await _setup_old_entry(hass)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.power_load_balancer_power_budget", "value": 6500},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert entry.data["power_budget_watt"] == 6500
    assert entry.options == {}


async def test_a_service_call_for_an_unmanaged_appliance_is_refused(
    hass, world, setup_balancer
):
    """Reporting success for something nothing manages hides a typo."""
    await setup_balancer()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "turn_off_appliance",
            {"entity_id": "switch.not_managed", "reason": "test"},
            blocking=True,
        )


async def test_services_go_with_the_last_entry(hass, world, setup_balancer):
    """Services that could only fail must not outlive every balancer."""
    entry = await setup_balancer()
    assert hass.services.has_service(DOMAIN, "turn_off_appliance")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "turn_off_appliance")
    assert not hass.services.has_service(DOMAIN, "turn_on_appliance")
