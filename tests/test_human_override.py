"""
Someone wants hot water while the balancer is holding a tank off.

The appliance clears its own shed when a person asks for heat, and it does so
without changing the mode it reports -- the shed and the release look
identical from the outside except for one attribute. Anything that watches
only the reported mode sees no change at all, and then acts on a picture of
the house that is a shed out of date.
"""

from __future__ import annotations

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


async def _shed_the_upstairs_tank(hass, clock, world) -> None:
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)
    # Shedding worked: the tank stops drawing and the house falls back.
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL - TANK_VA, 30, clock)


def _clear_shed_by_hand(hass) -> None:
    """Ask for heat by hand; the appliance drops the shed, mode unchanged."""
    state = hass.states.get(UPSTAIRS)
    attributes = dict(state.attributes)
    attributes["load_shed"] = False
    hass.states.async_set(UPSTAIRS, state.state, attributes)


async def test_clearing_a_shed_by_hand_is_noticed(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """The balancer must stop believing it still owns the tank."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    await _shed_the_upstairs_tank(hass, clock, world)

    controller = balancer_of(hass, entry)._appliance_controller
    assert UPSTAIRS in controller.get_balanced_off_appliances()

    _clear_shed_by_hand(hass)
    await hass.async_block_till_done()

    assert UPSTAIRS not in controller.get_balanced_off_appliances(), (
        "kept holding a tank the owner had already taken back"
    )


async def test_clearing_a_shed_by_hand_raises_no_false_alarm(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    The regression this guards: paging the owner for their own action.

    Left unnoticed, the tank stays on the balanced-off list while reporting
    that it is running again, which is exactly the shape of a shed that did
    not take effect -- and the owner gets told so, every time they want a
    shower during an overload.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    events: list[str] = []
    hass.bus.async_listen(
        "power_load_balancer_event", lambda e: events.append(e.data["message"])
    )

    await _shed_the_upstairs_tank(hass, clock, world)
    _clear_shed_by_hand(hass)
    await hass.async_block_till_done()

    # Well past the window in which an ineffective shed would be reported.
    await clock.advance(300)
    await hold(hass, 3000, 120, clock)

    assert not [m for m in events if "did not take effect" in m], events


async def test_owner_wins_but_the_balancer_re_sheds_if_still_over(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Taking the tank back is allowed; keeping it is not, while over."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _shed_the_upstairs_tank(hass, clock, world)
    world.clear()

    _clear_shed_by_hand(hass)
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()

    # The rest of the house keeps the load over the line on its own.
    await hold(hass, ARM_LEVEL + 60, 120, clock)

    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS), (
        "the tank came back and the house stayed over, but nothing re-shed it"
    )


async def test_owner_keeps_the_tank_when_the_house_is_quiet(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """No overload, no reason to take it away again."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _shed_the_upstairs_tank(hass, clock, world)
    world.clear()

    await hold(hass, 2500, 60, clock)
    world.clear()

    _clear_shed_by_hand(hass)
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()

    await hold(hass, 2500 + TANK_VA, 300, clock)

    assert world.of(UPSTAIRS) == [], "re-shed a tank while the house was quiet"


async def test_veto_sizes_an_unmeasured_tank_from_its_own_rating(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A tank's power sensor lags by a minute; its rating does not.

    Straight after the owner takes the tank back its sensor still reads zero,
    so a decision made on the sensor alone believes the tank is free. The
    appliance's declared rating answers immediately.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer(power_sensors=ladder(upstairs_nameplate=2500))

    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)

    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS), (
        "a tank reporting no power yet was treated as costing nothing to keep"
    )


async def test_a_rating_of_zero_means_unknown_not_free(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An unset rating must not be read as an appliance that draws nothing."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    await hass.async_block_till_done()

    controller = balancer_of(hass, entry)._appliance_controller
    assert controller.get_nominal_power(UPSTAIRS) == 0.0

    # It is still a shed candidate; the balancer simply cannot size it.
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)


async def test_release_lag_is_not_mistaken_for_a_failed_restore(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A released tank may take minutes to draw again; that is normal.

    The appliance holds a minimum off period, so after a release the element
    stays cold for a while and its power sensor keeps reading zero. Nothing
    should read that quiet period as a restore that failed.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    events: list[str] = []
    hass.bus.async_listen(
        "power_load_balancer_event", lambda e: events.append(e.data["message"])
    )

    await _shed_the_upstairs_tank(hass, clock, world)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await clock.advance(600)
    await hold(hass, 2000, 120, clock)
    assert ("generic_water_heater", "release") in world.of(UPSTAIRS)

    # Four minutes of the element staying cold after the release.
    events.clear()
    await hold(hass, 2000, 240, clock)

    assert events == [], f"complained about a normal minimum-off period: {events}"
