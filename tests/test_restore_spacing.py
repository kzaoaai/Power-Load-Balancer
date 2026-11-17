"""
Restores come back one at a time, and say why they are still waiting.

With room for two, both tanks came back in consecutive evaluations, before
the meter could show the first. And a released tank does not necessarily draw
at once: generic water heater staggers its tanks, and one at temperature does
not heat at all. Its meter reading of zero then looked like headroom, so the
second tank was sized against power the first was about to take.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    DOWNSTAIRS_POWER,
    HEATER,
    MAIN_SENSOR,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    balancer_of,
    hold,
    ladder,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

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


def _a_reading_mid_call(hass: HomeAssistant) -> None:
    """Publish one meter reading from inside a turn-on call."""
    hass.states.async_set(
        MAIN_SENSOR,
        "1003",
        {"unit_of_measurement": "VA", "device_class": "apparent_power"},
    )


def _release_seeing_a_reading(
    hass: HomeAssistant, world, *, confirmed_first: bool
) -> None:
    """
    Make the first release slow enough for a meter reading to land in it.

    A cloud device or a relay answers a turn-on in seconds, against a meter
    that reports every five to ten. The reading lands either before the tank
    reports itself released or after; each used to start a second restore
    while the first was still in flight.
    """
    seen = {"reading": False}

    async def _release(call: ServiceCall) -> None:
        entity_ids = call.data["entity_id"]
        for entity_id in [entity_ids] if isinstance(entity_ids, str) else entity_ids:
            world.calls.append(("generic_water_heater", "release", entity_id))
            state = hass.states.get(entity_id)
            attributes = {**state.attributes, "load_shed": False}
            if confirmed_first:
                hass.states.async_set(entity_id, state.state, attributes)
            if not seen["reading"]:
                seen["reading"] = True
                _a_reading_mid_call(hass)
                for _ in range(20):
                    await asyncio.sleep(0)
            if not confirmed_first:
                hass.states.async_set(entity_id, state.state, attributes)

    hass.services.async_register("generic_water_heater", "release", _release)


@pytest.mark.parametrize(
    "confirmed_first", [False, True], ids=["before_confirm", "after_confirm"]
)
async def test_a_reading_during_a_restore_call_starts_no_second_restore(  # noqa: PLR0913, PLR0917
    hass, world, clock, setup_balancer, register_shed_platform, confirmed_first
):
    """Before: the same tank sent twice, or both tanks back at once."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await hold(hass, 1000, 10, clock)
    await _both_tanks_shed(hass, world, clock)
    _release_seeing_a_reading(hass, world, confirmed_first=confirmed_first)

    await hold(hass, 1000, 20, clock)
    assert _releases(world) == [DOWNSTAIRS], _releases(world)

    # Once the first has landed, the spacing runs as before.
    await hold(hass, 1000, 20, clock)
    assert _releases(world) == [DOWNSTAIRS, UPSTAIRS], _releases(world)


async def test_a_reading_during_a_timer_restore_starts_no_second_restore(  # noqa: PLR0913, PLR0917
    hass, world, clock, setup_balancer, register_shed_platform, monkeypatch
):
    """The cooldown timer's restore holds the meter's back while in flight."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer(power_sensors=ladder(include_heater=True))
    controller = balancer_of(hass, entry)._appliance_controller
    await hold(hass, 1000, 10, clock)
    await _both_tanks_shed(hass, world, clock)

    turn_on = controller.turn_on_appliance_service
    seen = {"reading": False}

    async def _slow_turn_on(entity_id: str, reason: str | None = None) -> bool:
        if not seen["reading"]:
            seen["reading"] = True
            _a_reading_mid_call(hass)
            for _ in range(20):
                await asyncio.sleep(0)
        return await turn_on(entity_id, reason)

    monkeypatch.setattr(controller, "turn_on_appliance_service", _slow_turn_on)
    await _timer_restore(hass, controller, 1000)
    # The timer is a background task, and the slow call outlives the plain
    # wait inside the helper.
    await hass.async_block_till_done(wait_background_tasks=True)

    # Home Assistant's own switch service replaces the recorder's once the
    # balancer's switch platform loads, so the heater shows in the log only.
    log = hass.states.get(LOG_SENSOR).attributes["events"]
    assert any(f"Turned on {HEATER}" in line for line in log), log
    assert _releases(world) == [], _releases(world)


async def test_a_timer_restore_that_could_do_nothing_holds_no_headroom(
    hass, world, clock, setup_balancer, monkeypatch
):
    """Nothing came on: no hold, no spacing, its size kept, another cooldown."""
    entry = await setup_balancer(power_sensors=ladder(include_heater=True))
    controller = balancer_of(hass, entry)._appliance_controller

    async def _could_not(entity_id: str, reason: str | None = None) -> bool:
        return False

    monkeypatch.setattr(controller, "turn_on_appliance_service", _could_not)
    controller.mark_appliance_balanced_off(HEATER, "test shed")
    controller.schedule_auto_turn_on(HEATER, 1000, lambda: 3000.0, lambda: ARM_LEVEL)
    await clock.advance(121)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=121), fire_all=True
    )
    await hass.async_block_till_done()

    snapshot = controller.get_diagnostics_snapshot()
    assert HEATER not in snapshot["settling_restores"], snapshot
    assert snapshot["restores_in_flight_watt"] == {}, snapshot
    assert controller.restore_spacing_remaining() == 0
    assert HEATER in controller.get_balanced_off_appliances()
    assert controller.get_balanced_off_age(HEATER) == 0, "cooldown not restarted"
    assert controller.get_expected_power_restoration(HEATER) == 1000
    assert HEATER not in controller._own_restores
