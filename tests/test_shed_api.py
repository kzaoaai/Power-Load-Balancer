"""
Shedding an integration that owns its own suppression semantics.

Some integrations expose dedicated shed/release services because an
entity-level set_operation_mode is indistinguishable from a person at the
wall, and being mistaken for one has side effects (an eco policy pausing for
hours). Those integrations also tend to keep reporting the operation mode the
user chose while shed, flagging the suppression in an attribute instead --
which quietly breaks any check that asks "is this appliance off?".
"""

from __future__ import annotations

from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    UPSTAIRS,
    UPSTAIRS_POWER,
    balancer_of,
    hold,
)


async def _drive_over_arm(hass, clock, seconds: float = 75) -> None:
    """Hold the load above the arm level long enough for the dwell to elapse."""
    await hold(hass, ARM_LEVEL + 60, seconds, clock)


async def _make_active(hass, entity_id: str, power_sensor: str) -> None:
    """Put an appliance into a drawing state with a matching power reading."""
    hass.states.async_set(
        entity_id, "electric", {"operation_list": ["electric", "off"]}
    )
    hass.states.async_set(power_sensor, "2050", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()


async def test_uses_shed_service_not_operation_mode(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A managed shed must never go through set_operation_mode."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _make_active(hass, UPSTAIRS, UPSTAIRS_POWER)

    await _drive_over_arm(hass, clock)

    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)
    assert "set_operation_mode" not in world.services_for(UPSTAIRS)


async def test_no_operation_mode_is_stored_for_managed_sheds(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Storing a mode would leave a stale entry and a redundant restore call."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    await _make_active(hass, UPSTAIRS, UPSTAIRS_POWER)

    await _drive_over_arm(hass, clock)

    controller = balancer_of(hass, entry)._appliance_controller
    assert UPSTAIRS not in controller._previous_operation_modes


async def test_falls_back_when_services_are_absent(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An older install of the appliance integration must still be sheddable."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS, with_services=False)
    await setup_balancer()
    await _make_active(hass, UPSTAIRS, UPSTAIRS_POWER)

    await _drive_over_arm(hass, clock)

    assert "set_operation_mode" in world.services_for(UPSTAIRS)
    assert not [c for c in world.calls if c[0] == "generic_water_heater"]


async def test_shed_appliance_still_reporting_its_mode_is_released(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    The regression that matters: shed forever because the state is not 'off'.

    A managed shed leaves the entity reporting 'electric'. If the balancer
    judges restorability on that state alone it never offers the appliance for
    release, and the tank stays cold until a person notices.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    await _make_active(hass, UPSTAIRS, UPSTAIRS_POWER)

    await _drive_over_arm(hass, clock)
    assert ("generic_water_heater", "shed") in world.of(UPSTAIRS)

    shed_state = hass.states.get(UPSTAIRS)
    assert shed_state.state == "electric", "managed shed should not change the mode"
    assert shed_state.attributes["load_shed"] is True

    balancer = balancer_of(hass, entry)
    assert balancer._appliance_controller.is_appliance_shed(UPSTAIRS) is True

    # The load drops away and the cooldown expires; the release must happen.
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await clock.advance(600)
    await hold(hass, 2500, 40, clock)

    assert ("generic_water_heater", "release") in world.of(UPSTAIRS)


async def test_shed_appliance_is_not_shed_twice(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An already-shed appliance must not be picked as a shed candidate again."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _make_active(hass, UPSTAIRS, UPSTAIRS_POWER)

    await _drive_over_arm(hass, clock, 200)

    assert world.count(UPSTAIRS, "shed") == 1, world.of(UPSTAIRS)


async def test_a_shed_that_does_not_take_effect_is_surfaced(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A silently ignored shed must be reported, and reported only once."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS, with_services=False)
    await setup_balancer()
    await _make_active(hass, UPSTAIRS, UPSTAIRS_POWER)

    events: list[str] = []

    def _capture(event) -> None:
        events.append(event.data["message"])

    hass.bus.async_listen("power_load_balancer_event", _capture)

    await _drive_over_arm(hass, clock)
    # The appliance ignored the command and is still reporting electric.
    await clock.advance(600)
    await hold(hass, ARM_LEVEL + 60, 30, clock)
    await hass.async_block_till_done()

    ineffective = [m for m in events if "did not take effect" in m]
    assert len(ineffective) == 1, events
    assert UPSTAIRS in ineffective[0]


async def test_effective_shed_is_never_reported_as_ineffective(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """The attribute-only shed must not be mistaken for a failed shed."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _make_active(hass, UPSTAIRS, UPSTAIRS_POWER)

    events: list[str] = []
    hass.bus.async_listen(
        "power_load_balancer_event", lambda e: events.append(e.data["message"])
    )

    await _drive_over_arm(hass, clock)
    await clock.advance(600)
    await hold(hass, ARM_LEVEL + 60, 30, clock)
    await hass.async_block_till_done()

    assert not [m for m in events if "did not take effect" in m], events
