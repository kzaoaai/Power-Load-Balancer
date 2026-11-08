"""
What the balancer finds when it comes back up.

Which appliances the balancer had shed is kept in memory and in a saved
record, and a managed shed also lives in the appliance itself. A restart must
reconcile all three, or an appliance the balancer turned off stays off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_load_balancer.const import DOMAIN, shed_store_key
from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    balancer_of,
    hold,
    ladder,
)

if TYPE_CHECKING:
    from homeassistant.core import ServiceCall


async def _restart(hass, entry: MockConfigEntry) -> None:
    """Reload the entry the way a Home Assistant restart would."""
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_shed_appliance_is_adopted_after_a_restart(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A tank left shed across a restart must still come back.

    The balanced-off bookkeeping is in memory only. If nothing reconciles it
    against what the appliances report, a tank shed just before a restart is
    never offered for release again and simply stays cold.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()

    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)

    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await _restart(hass, entry)
    world.clear()

    balancer = balancer_of(hass, entry)
    assert UPSTAIRS in balancer._appliance_controller.get_balanced_off_appliances(), (
        "a tank still reporting load_shed was not adopted after the restart"
    )

    await clock.advance(600)
    await hold(hass, 2000, 120, clock)

    assert ("generic_water_heater", "release") in world.of(UPSTAIRS), (
        "tank left shed forever across a restart"
    )


async def test_appliance_not_shed_is_not_adopted(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Adoption must key off the appliance's own report, nothing else."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    hass.states.async_set(
        UPSTAIRS,
        "electric",
        {"operation_list": ["electric", "off"], "load_shed": False},
    )
    await _restart(hass, entry)

    balancer = balancer_of(hass, entry)
    assert balancer._appliance_controller.get_balanced_off_appliances() == []


async def test_restart_with_no_shed_support_adopts_nothing(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An appliance without a shed attribute must not be adopted by accident."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS, with_services=False)
    entry = await setup_balancer()
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    await _restart(hass, entry)

    balancer = balancer_of(hass, entry)
    assert balancer._appliance_controller.get_balanced_off_appliances() == []


async def test_options_change_does_not_strand_a_shed_appliance(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Editing the budget reloads the entry; the shed must survive that too."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()

    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)

    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "power_budget_watt": 6500}
    )
    await hass.async_block_till_done()
    world.clear()

    await clock.advance(600)
    await hold(hass, 1500, 120, clock)

    assert ("generic_water_heater", "release") in world.of(UPSTAIRS)


async def test_setup_is_clean_when_nothing_was_shed(hass, world, setup_balancer):
    """The common restart: nothing outstanding, nothing to reconcile."""
    entry = await setup_balancer()
    balancer = balancer_of(hass, entry)
    assert balancer._appliance_controller.get_balanced_off_appliances() == []
    assert hass.data[DOMAIN][entry.entry_id] is balancer


async def test_adopted_appliance_is_not_restored_into_a_busy_house(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    An unknown-size restore must wait for real quiet, not merely headroom.

    The adopted tank's draw is unknown, so restoring it at, say, 5 kW could
    put the house straight back over the arm level and start a slow
    shed/restore cycle on a physical contactor.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()

    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await _restart(hass, entry)
    world.clear()

    # Under the 8000 budget and even under the 6240 arm level, but nowhere
    # near quiet enough to absorb an appliance of unknown size.
    await clock.advance(600)
    await hold(hass, 5000, 300, clock)
    assert world.of(UPSTAIRS) == [], "restored an unknown-size load into a busy house"

    # Now the house really is quiet.
    await hold(hass, 1500, 120, clock)
    assert ("generic_water_heater", "release") in world.of(UPSTAIRS)


RADIATOR = "climate.spare_radiator"
RADIATOR_POWER = "sensor.spare_radiator_power"
RADIATOR_ATTRIBUTES = {"hvac_modes": ["off", "heat", "auto"], "hvac_mode": "auto"}


async def _shed_a_running_radiator(hass, clock, setup_balancer):
    """
    Start a balancer with a radiator running, overload the house, shed it.

    Returns the entry and the list of HVAC modes the balancer asked for.
    """
    modes: list[str] = []

    async def _set_hvac_mode(call: ServiceCall) -> None:
        modes.append(call.data["hvac_mode"])

    hass.services.async_register("climate", "set_hvac_mode", _set_hvac_mode)
    hass.states.async_set(RADIATOR, "auto", RADIATOR_ATTRIBUTES)
    hass.states.async_set(RADIATOR_POWER, "1500", {"unit_of_measurement": "W"})
    rung = {
        "appliance": RADIATOR,
        "entity_id": RADIATOR_POWER,
        "name": "Spare Radiator",
        "importance": 3,
        "last_resort": False,
    }
    entry = await setup_balancer(power_sensors=[*ladder(), rung])

    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert modes == ["off"]
    # The recorder only records; play the radiator's part.
    hass.states.async_set(RADIATOR, "off", {**RADIATOR_ATTRIBUTES, "hvac_mode": "off"})
    hass.states.async_set(RADIATOR_POWER, "0", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    return entry, modes


async def test_a_radiator_shed_before_a_restart_comes_back_in_its_own_mode(
    hass, world, clock, setup_balancer
):
    """
    "off" alone does not say the balancer did it; the saved record does.

    Nothing reported the shed after the restart, so a radiator or a switch
    the balancer had turned off stayed off for good, and the mode to give
    back was lost with it.
    """
    entry, modes = await _shed_a_running_radiator(hass, clock, setup_balancer)

    await _restart(hass, entry)
    balancer = balancer_of(hass, entry)
    assert RADIATOR in balancer._appliance_controller.get_balanced_off_appliances(), (
        "forgot a radiator it had shed before the restart"
    )

    await clock.advance(600)
    await hold(hass, 1500, 120, clock)
    assert modes == ["off", "auto"], f"restored the wrong mode: {modes}"


async def test_a_radiator_someone_turned_back_on_is_not_adopted(
    hass, world, clock, setup_balancer
):
    """A saved record is evidence of the past; a running appliance outranks it."""
    entry, _ = await _shed_a_running_radiator(hass, clock, setup_balancer)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(
        RADIATOR, "heat", {**RADIATOR_ATTRIBUTES, "hvac_mode": "heat"}
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    balancer = balancer_of(hass, entry)
    assert RADIATOR not in balancer._appliance_controller.get_balanced_off_appliances()


async def test_removing_the_entry_deletes_its_shed_record(
    hass, hass_storage, world, clock, setup_balancer
):
    """A removed balancer must not leave its record behind."""
    entry, _ = await _shed_a_running_radiator(hass, clock, setup_balancer)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    key = shed_store_key(entry.entry_id)
    assert RADIATOR in hass_storage[key]["data"]["appliances"]

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
