"""
What the balancer finds when it comes back up.

Which appliances the balancer had shed lives only in memory, but the shed
itself lives in the appliance. A restart therefore lands the two out of step,
and the appliance is the one holding a tank cold.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_load_balancer.const import DOMAIN
from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    balancer_of,
    hold,
)


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
