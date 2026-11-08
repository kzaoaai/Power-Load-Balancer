"""
Which appliance comes back first, and how soon after the budget recovers.

The ladder sheds the least important appliance first. Restoring in the order
things were shed brought the least important back first whenever there was
room for only one. And a budget that dropped for a few seconds, a brief switch
to the generator, kept what it shed off for a full cooldown after the budget
was already back.
"""

from __future__ import annotations

from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    DOWNSTAIRS_POWER,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    hold,
)

BUDGET_ENTITY = "number.power_load_balancer_power_budget"


async def _running(hass, tank: str, sensor: str) -> None:
    hass.states.async_set(tank, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(sensor, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()


async def test_the_most_important_appliance_comes_back_first(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """With room for one tank, the one shed last (the more important) returns."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    # A quiet house first, so turning both tanks on does not trip the veto.
    await hold(hass, 1000, 10, clock)
    await _running(hass, UPSTAIRS, UPSTAIRS_POWER)
    await _running(hass, DOWNSTAIRS, DOWNSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    await hold(hass, ARM_LEVEL + 60, 30, clock)
    sheds = [entity for _, service, entity in world.calls if service == "shed"]
    assert sheds == [UPSTAIRS, DOWNSTAIRS], sheds

    for sensor in (UPSTAIRS_POWER, DOWNSTAIRS_POWER):
        hass.states.async_set(sensor, "0", {"unit_of_measurement": "W"})
    world.clear()
    await clock.advance(600)
    # Room for one tank and the restore margin, not for two.
    await hold(hass, 3900, 20, clock)

    releases = [entity for _, service, entity in world.calls if service == "release"]
    assert releases[:1] == [DOWNSTAIRS], (
        f"brought the less important tank back first: {releases}"
    )


async def test_a_budget_back_up_returns_what_its_drop_shed_without_a_cooldown(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A few seconds on a lower budget must not cost a whole cooldown."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _running(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, 4500, 20, clock)
    world.clear()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": BUDGET_ENTITY, "value": 4400},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS), (
        "did not shed at once when the budget fell below the load"
    )

    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await clock.advance(10)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": BUDGET_ENTITY, "value": 8000},
        blocking=True,
    )
    await hold(hass, 2450, 20, clock)

    assert ("generic_water_heater", "release") in world.of(UPSTAIRS), (
        "kept the tank off for a cooldown after the budget was back"
    )


async def test_a_restore_keeps_a_margin_below_the_ceiling(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Landing exactly on the effective budget would arm the dwell at once."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _running(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})

    # Exactly the tank's draw of headroom: not enough once the margin counts.
    await hold(hass, ARM_LEVEL - TANK_VA, 400, clock)
    assert ("generic_water_heater", "release") not in world.of(UPSTAIRS)

    await hold(hass, ARM_LEVEL - TANK_VA - 300, 20, clock)
    assert ("generic_water_heater", "release") in world.of(UPSTAIRS)
