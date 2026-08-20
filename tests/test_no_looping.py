"""
Scenarios where the balancer could cycle a real contactor.

Every shed and restore here is a physical relay on a water tank. A logic bug
that oscillates costs contactor life and hot water, and the load reading that
would reveal it arrives seconds late, so these cases are cheaper to pin down
in a test than on the hardware.
"""

from __future__ import annotations

from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    DOWNSTAIRS_POWER,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    balancer_of,
    feed,
    hold,
)


async def _tank_drawing(hass, entity: str, power_sensor: str) -> None:
    hass.states.async_set(entity, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(power_sensor, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()


async def _shed_upstairs(hass, clock, world) -> None:
    """Drive one sustained episode that sheds the upstairs tank."""
    await _tank_drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()


async def test_restore_does_not_immediately_re_shed(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A restore into a load that would re-arm is the classic relay chatter.

    Restoring must be judged against the arm level, not the raw budget: the
    tank draws about 2 kW, so coming back at 5 kW would put the house at 7 kW,
    over the 6240 arm level, and the next dwell would shed it straight again.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _shed_upstairs(hass, clock, world)
    world.clear()

    # Comfortably under the 8000 budget but not under arm level once restored.
    await clock.advance(600)
    await hold(hass, 5000, 300, clock)

    assert world.of(UPSTAIRS) == [], (
        "restored into a load that would immediately re-shed"
    )


async def test_full_cycle_settles_without_chatter(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Shed, genuine recovery, restore -- and then stay put."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _shed_upstairs(hass, clock, world)
    world.clear()

    await clock.advance(600)
    await hold(hass, 2000, 120, clock)
    assert ("generic_water_heater", "release") in world.of(UPSTAIRS), "never restored"

    # The tank comes back and draws again, but the house is still quiet.
    world.clear()
    await _tank_drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, 2000 + TANK_VA, 300, clock)

    assert world.of(UPSTAIRS) == [], f"chattered after settling: {world.of(UPSTAIRS)}"


async def test_repeated_episodes_do_not_shed_more_than_the_ladder(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A long overload sheds each rung once, not each rung repeatedly."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _tank_drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await _tank_drawing(hass, DOWNSTAIRS, DOWNSTAIRS_POWER)

    await hold(hass, ARM_LEVEL + 400, 900, clock)

    assert world.count(UPSTAIRS, "shed") == 1, world.of(UPSTAIRS)
    assert world.count(DOWNSTAIRS, "shed") == 1, world.of(DOWNSTAIRS)


async def test_nothing_left_to_shed_is_reported_once(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An unshakeable overload must not spam notifications every escalation."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    events: list[str] = []
    hass.bus.async_listen(
        "power_load_balancer_event", lambda e: events.append(e.data["message"])
    )

    # No appliance is drawing, so nothing can be shed however high the load is.
    await hold(hass, ARM_LEVEL + 1000, 600, clock)

    unable = [m for m in events if "Unable to shed" in m]
    assert len(unable) <= 1, unable


async def test_load_straddling_the_arm_level_does_not_shed(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Load bouncing across the line is not a sustained overload."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _tank_drawing(hass, UPSTAIRS, UPSTAIRS_POWER)

    for _ in range(10):
        await hold(hass, ARM_LEVEL + 50, 20, clock)
        await hold(hass, ARM_LEVEL - 400, 20, clock)

    assert world.of(UPSTAIRS) == [], "shed on a load that kept dropping below arm"


async def test_single_spike_does_not_shed(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """One high sample below the hard budget is a kettle, not an overload."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _tank_drawing(hass, UPSTAIRS, UPSTAIRS_POWER)

    await hold(hass, 4000, 60, clock)
    await feed(hass, ARM_LEVEL + 1000)  # 7240: over arm, under the 8000 budget
    await clock.advance(11)
    await hold(hass, 4000, 120, clock)

    assert world.of(UPSTAIRS) == []


async def test_single_sample_over_the_hard_budget_sheds_at_once(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    Past the real limit there is nothing to wait for.

    The dwell exists to avoid shedding on transients below the ceiling. Above
    the budget the inverter is the thing at risk, so the emergency path must
    not wait out a dwell.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _tank_drawing(hass, UPSTAIRS, UPSTAIRS_POWER)

    await hold(hass, 4000, 30, clock)
    world.clear()
    await feed(hass, 8600)

    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)


async def test_disabling_mid_episode_stops_shedding(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Turning the balancer off must not leave a countdown primed."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    await _tank_drawing(hass, UPSTAIRS, UPSTAIRS_POWER)

    await hold(hass, ARM_LEVEL + 60, 40, clock)  # armed, dwell not yet elapsed
    balancer = balancer_of(hass, entry)
    assert balancer.get_diagnostics_snapshot()["sustained_shedding"]["armed"] is True

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.power_load_balancer_enabled"},
        blocking=True,
    )
    await hass.async_block_till_done()
    world.clear()

    await clock.advance(3600)
    await hold(hass, ARM_LEVEL + 60, 300, clock)

    assert world.of(UPSTAIRS) == [], "shed while disabled"

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.power_load_balancer_enabled"},
        blocking=True,
    )
    await hass.async_block_till_done()
    await feed(hass, ARM_LEVEL + 61)
    await clock.advance(11)
    await feed(hass, ARM_LEVEL + 62)

    assert world.of(UPSTAIRS) == [], "re-enable shed instantly instead of re-arming"


async def test_main_sensor_dropout_does_not_shed_on_stale_data(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A dead meter must stop the clock, not keep counting a frozen reading."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    await _tank_drawing(hass, UPSTAIRS, UPSTAIRS_POWER)

    await hold(hass, ARM_LEVEL + 60, 30, clock)
    hass.states.async_set("sensor.inverter_load_apparent_power", "unavailable")
    await hass.async_block_till_done()

    await clock.advance(600)
    hass.states.async_set(DOWNSTAIRS_POWER, "1", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()

    assert world.of(UPSTAIRS) == [], "shed against a stale reading"
    snapshot = balancer_of(hass, entry).get_diagnostics_snapshot()
    assert snapshot["sustained_shedding"]["armed"] is False
