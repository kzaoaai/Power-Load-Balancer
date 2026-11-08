"""
Restores come back one at a time, and say why they are still waiting.

With room for two, both tanks came back in consecutive evaluations, before
the meter could show the first. And a released tank does not necessarily draw
at once: generic water heater staggers its tanks, and one at temperature does
not heat at all. Its meter reading of zero then looked like headroom, so the
second tank was sized against power the first was about to take.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    DOWNSTAIRS_POWER,
    HEATER,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    balancer_of,
    hold,
    ladder,
)

PENDING = "sensor.power_load_balancer_pending_actions"
LOG_SENSOR = "sensor.power_load_balancer_log"


async def _both_tanks_shed(hass, world, clock) -> None:
    for tank, sensor in ((UPSTAIRS, UPSTAIRS_POWER), (DOWNSTAIRS, DOWNSTAIRS_POWER)):
        hass.states.async_set(tank, "electric", {"operation_list": ["electric", "off"]})
        hass.states.async_set(sensor, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    await hold(hass, ARM_LEVEL + 60, 30, clock)
    assert world.services_for(UPSTAIRS) == ["shed"]
    assert world.services_for(DOWNSTAIRS) == ["shed"]

    # Released tanks that do not heat yet: their meters stay at zero.
    for sensor in (UPSTAIRS_POWER, DOWNSTAIRS_POWER):
        hass.states.async_set(sensor, "0", {"unit_of_measurement": "W"})
    world.clear()
    await clock.advance(600)


def _releases(world) -> list[str]:
    return [entity for _, service, entity in world.calls if service == "release"]


async def test_restores_are_spaced_out(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Room for both still brings them back one at a time."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await hold(hass, 1000, 10, clock)
    await _both_tanks_shed(hass, world, clock)

    await hold(hass, 1000, 20, clock)
    assert _releases(world) == [DOWNSTAIRS], _releases(world)

    await hold(hass, 1000, 20, clock)
    assert _releases(world) == [DOWNSTAIRS, UPSTAIRS], _releases(world)


async def test_a_released_tank_not_yet_heating_keeps_its_headroom(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Room for one: the idle first tank's draw is held, not handed out again."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await hold(hass, 1000, 10, clock)
    await _both_tanks_shed(hass, world, clock)

    await hold(hass, 3000, 100, clock)
    assert _releases(world) == [DOWNSTAIRS], (
        f"sized the second tank against the first one's headroom: {_releases(world)}"
    )

    # A tank that never starts heating must not block the next one for good.
    await hold(hass, 3000, 80, clock)
    assert _releases(world) == [DOWNSTAIRS, UPSTAIRS], _releases(world)


async def test_a_restored_tank_seen_drawing_ends_its_hold(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Once the meter carries the first tank, it is not counted twice."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    await hold(hass, 1000, 10, clock)
    await _both_tanks_shed(hass, world, clock)

    await hold(hass, 1000, 20, clock)
    assert _releases(world) == [DOWNSTAIRS]
    hass.states.async_set(DOWNSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, 1000 + TANK_VA, 40, clock)

    assert _releases(world) == [DOWNSTAIRS, UPSTAIRS], _releases(world)
    settling = balancer_of(
        hass, entry
    )._appliance_controller.get_diagnostics_snapshot()["settling_restores"]
    assert DOWNSTAIRS not in settling, settling


async def test_the_pending_sensor_says_why_a_tank_is_off(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Why it was shed, when, and what it waits for, in fields that stay put."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await hold(hass, 1000, 10, clock)
    await _both_tanks_shed(hass, world, clock)
    await hold(hass, 5000, 30, clock)

    pending = hass.states.get(PENDING).attributes["pending"]
    restores = {item["entity_id"]: item for item in pending}
    tank = restores[UPSTAIRS]
    assert tank["shed_reason"].startswith("Sustained load"), tank
    assert tank["shed_at"], tank
    margin = round(ARM_LEVEL * 0.02)
    assert tank["needs_watts"] == TANK_VA + margin, tank
    assert tank["reason"] == f"waiting for {TANK_VA + margin} W of headroom", tank

    await hold(hass, 1000, 10, clock)
    pending = hass.states.get(PENDING).attributes["pending"]
    waiting = next(item for item in pending if item["entity_id"] == UPSTAIRS)
    assert waiting["reason"] == f"waiting for {DOWNSTAIRS} to settle after its restore"
    assert waiting["due_at"] is not None


async def _timer_restore(hass, controller, expected: float) -> None:
    """Run a cooldown-timer restore for the spare heater to completion."""
    controller.mark_appliance_balanced_off(HEATER, "test shed")
    controller.schedule_auto_turn_on(
        HEATER, expected, lambda: 3000.0, lambda: ARM_LEVEL
    )
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=121), fire_all=True
    )
    await hass.async_block_till_done()


async def test_the_cooldown_timer_respects_spacing_and_the_hold(
    hass, world, clock, setup_balancer
):
    """The timer path is a restore like any other: spaced, and sized net of holds."""
    entry = await setup_balancer(power_sensors=ladder(include_heater=True))
    controller = balancer_of(hass, entry)._appliance_controller

    def _restored() -> bool:
        log = hass.states.get(LOG_SENSOR).attributes["events"]
        return any(f"Turned on {HEATER}" in line for line in log)

    # Another appliance came back a moment ago.
    controller.note_restore_settling(UPSTAIRS, TANK_VA)
    await _timer_restore(hass, controller, 1000)
    assert not _restored(), "the timer ignored the spacing"

    # Spacing over, but the tank's draw is still held: 3240 W free less 2050 W
    # held leaves too little for 1500 W plus the margin.
    await clock.advance(31)
    await _timer_restore(hass, controller, 1500)
    assert not _restored(), "the timer spent headroom on hold"

    # The hold runs out; the heater comes back and is itself held.
    await clock.advance(150)
    await _timer_restore(hass, controller, 1500)
    assert _restored()
    assert HEATER in controller.get_diagnostics_snapshot()["settling_restores"]
