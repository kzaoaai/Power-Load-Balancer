"""
The balancer must recognise the results of its own actions.

Two ways it used to misread itself, both seen on the production tanks:

- A shed whose service call failed after the shed had already landed was
  forgotten, because only an appliance reading "off" counted as stopped. A shed
  tank keeps reporting its operation mode, so the balancer dropped it from its
  books and never released it.
- A restore came back through the turn-on handler as if a person had switched
  the tank on, and the veto meant for people shed it again within the second.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError

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


async def test_a_shed_that_landed_but_errored_is_still_released(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A failed call that still shed the tank must not orphan the shed."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)

    async def _shed_then_fail(call: ServiceCall) -> None:
        world.calls.append(("generic_water_heater", "shed", UPSTAIRS))
        state = hass.states.get(UPSTAIRS)
        attributes = dict(state.attributes)
        attributes["load_shed"] = True
        hass.states.async_set(UPSTAIRS, state.state, attributes)
        msg = "relay acknowledgement timed out"
        raise HomeAssistantError(msg)

    hass.services.async_register("generic_water_heater", "shed", _shed_then_fail)
    entry = await setup_balancer()
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)

    assert hass.states.get(UPSTAIRS).attributes.get("load_shed") is True
    controller = balancer_of(hass, entry)._appliance_controller
    assert UPSTAIRS in controller.get_balanced_off_appliances(), (
        "forgot a tank whose shed landed before the call failed"
    )

    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hold(hass, 2000, 600, clock)

    assert ("generic_water_heater", "release") in world.of(UPSTAIRS), (
        "a quiet house never got the tank back"
    )


async def test_the_balancer_does_not_veto_its_own_restore(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A restore sized on the measured draw must not be undone by the rating.

    The tank measured 1800 W when it was shed but is rated 2050 W. The restore
    fits on the measured draw; the veto, sizing on the rating, did not, so it
    shed the tank the balancer had just released.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer(power_sensors=ladder(upstairs_nameplate=TANK_VA))
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, "1800", {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert world.services_for(UPSTAIRS) == ["shed"]

    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    # 1900 W of headroom: room for the measured 1800 W, not the rated 2050 W.
    await hold(hass, ARM_LEVEL - 1900, 400, clock)

    assert world.services_for(UPSTAIRS) == ["shed", "release"], (
        f"undid its own restore: {world.services_for(UPSTAIRS)}"
    )
    controller = balancer_of(hass, entry)._appliance_controller
    assert UPSTAIRS not in controller.get_balanced_off_appliances()


async def test_a_person_turning_on_into_an_overload_is_still_vetoed(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Skipping the veto for the balancer's restores must not skip it for people."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer(power_sensors=ladder(upstairs_nameplate=TANK_VA))
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL - 100, 30, clock)
    assert world.services_for(UPSTAIRS) == ["shed"]

    # Someone asks for heat by hand: the tank clears its own shed.
    state = hass.states.get(UPSTAIRS)
    attributes = dict(state.attributes)
    attributes["load_shed"] = False
    hass.states.async_set(UPSTAIRS, state.state, attributes)
    await hass.async_block_till_done()

    assert world.services_for(UPSTAIRS) == ["shed", "shed"], (
        "let a person push the house past its budget"
    )
    controller = balancer_of(hass, entry)._appliance_controller
    assert UPSTAIRS in controller.get_balanced_off_appliances()
    log = hass.states.get("sensor.power_load_balancer_log").attributes["events"]
    assert any("turn-on vetoed" in line for line in log), log
