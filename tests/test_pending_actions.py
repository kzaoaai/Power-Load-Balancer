"""
What the balancer is waiting to do, and when.

The event log answers what already happened. During an overload the question
people actually ask is the other one: is something about to switch, which
appliance, and how long have I got. That has to be answerable at a glance
without the entity rewriting its own history every few seconds while it waits.
"""

from __future__ import annotations

from datetime import datetime

from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    DOWNSTAIRS_POWER,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    feed,
    hold,
)

PENDING = "sensor.power_load_balancer_pending_actions"


async def _drawing(hass, entity: str, power_sensor: str) -> None:
    hass.states.async_set(entity, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(power_sensor, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()


def _pending(hass) -> list[dict]:
    return hass.states.get(PENDING).attributes["pending"]


async def test_idle_when_nothing_is_queued(hass, world, setup_balancer):
    """A quiet house should say so plainly."""
    await setup_balancer()
    state = hass.states.get(PENDING)
    assert state.state == "0"
    assert state.attributes["summary"] == "Nothing pending"
    assert state.attributes["next_due_at"] is None


async def test_an_armed_shed_names_the_appliance_and_its_due_time(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Knowing a shed is coming is only half of it; which one matters."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)

    await hold(hass, ARM_LEVEL + 60, 20, clock)

    state = hass.states.get(PENDING)
    assert state.state == "1"
    shed = next(item for item in _pending(hass) if item["action"] == "shed")
    assert shed["entity_id"] == UPSTAIRS, "did not say which appliance would go"
    assert shed["due_at"] is not None
    assert UPSTAIRS.split(".", 1)[-1] in state.attributes["summary"]


async def test_the_named_appliance_is_the_one_actually_shed(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """The forecast and the action must come from the same selection."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await _drawing(hass, DOWNSTAIRS, DOWNSTAIRS_POWER)
    # Bringing the second tank up trips the turn-on veto, which sheds it
    # straight away. That is a separate decision; start counting after it.
    world.clear()

    await hold(hass, ARM_LEVEL + 60, 20, clock)
    forecast = next(item for item in _pending(hass) if item["action"] == "shed")
    predicted = forecast["entity_id"]

    await hold(hass, ARM_LEVEL + 60, 60, clock)

    shed_calls = [c for c in world.calls if c[1] == "shed"]
    assert shed_calls, "nothing was shed"
    assert shed_calls[0][2] == predicted, (
        f"said it would shed {predicted} but shed {shed_calls[0][2]}"
    )


async def test_due_time_does_not_drift_while_waiting(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    The regression the design exists for: no history churn while waiting.

    Home Assistant writes a new row whenever a state or any attribute
    changes, so reporting a countdown would rewrite this entity every few
    seconds for the entire wait. A due time holds still.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)

    # Deliberately off a whole second, and stepping by a fraction: a due time
    # recomputed from two separately-quantised clocks flips between adjacent
    # seconds under exactly these conditions, and a second-aligned fake clock
    # hides it completely.
    await clock.advance(0.35)
    await hold(hass, ARM_LEVEL + 60, 15, clock, interval=2.3)
    first = hass.states.get(PENDING)
    first_due = first.attributes["next_due_at"]
    first_written = first.last_updated

    await hold(hass, ARM_LEVEL + 60, 25, clock, interval=2.3)
    later = hass.states.get(PENDING)

    assert later.attributes["next_due_at"] == first_due, "the due time moved"
    assert later.last_updated == first_written, (
        "the entity rewrote itself while simply waiting"
    )


async def test_due_time_is_a_real_timestamp(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A dashboard has to be able to parse it."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 20, clock)

    due = hass.states.get(PENDING).attributes["next_due_at"]
    assert datetime.fromisoformat(due)


async def test_clears_once_the_load_drops(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A shed that is no longer coming must stop being advertised."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)

    await hold(hass, ARM_LEVEL + 60, 20, clock)
    assert hass.states.get(PENDING).state == "1"

    await hold(hass, 2000, 40, clock)

    assert hass.states.get(PENDING).state == "0"
    assert hass.states.get(PENDING).attributes["summary"] == "Nothing pending"


async def test_a_shed_appliance_is_queued_for_restore(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """After a shed, the waiting thing is the restore."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hold(hass, 2000, 20, clock)

    restore = next(item for item in _pending(hass) if item["action"] == "restore")
    assert restore["entity_id"] == UPSTAIRS
    assert restore["due_at"] is not None, "a cooldown is running, so it has a due time"
    assert "cooldown" in restore["reason"]


async def test_waiting_for_headroom_has_no_due_time(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Some waits have no clock: they end when the house gets quieter."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})

    # Past the cooldown, but the house is still too busy to take it back.
    await clock.advance(600)
    await hold(hass, 5000, 30, clock)

    restore = next(item for item in _pending(hass) if item["action"] == "restore")
    assert restore["due_at"] is None
    assert "headroom" in restore["reason"]


async def test_an_ineffective_shed_is_flagged_for_attention(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An appliance that ignored a shed is pending a person, not a timer."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS, with_services=False)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 75, clock)

    # The appliance kept running regardless of the command.
    await clock.advance(600)
    await hold(hass, ARM_LEVEL + 60, 30, clock)

    attention = [
        item
        for item in _pending(hass)
        if item["action"] == "attention" and item["entity_id"] == UPSTAIRS
    ]
    assert attention, _pending(hass)
    assert attention[0]["due_at"] is None
    assert "did not take effect" in attention[0]["reason"]


async def test_nothing_is_queued_while_disabled(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A disabled balancer is not about to do anything."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 20, clock)
    assert hass.states.get(PENDING).state == "1"

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.power_load_balancer_enabled"},
        blocking=True,
    )
    await feed(hass, ARM_LEVEL + 61)
    await hass.async_block_till_done()

    assert hass.states.get(PENDING).state == "0"


async def test_an_unresolved_shed_survives_the_load_dropping(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    An appliance ignoring a shed is still a problem once load falls.

    The sustained tracker disarms whenever the load drops below the arm
    level, which is most of the time. If disarming also forgets which sheds
    were ignored, the flag is erased before anyone can see it -- and the
    once-only warning re-fires every cycle, because the record of having
    already warned went with it.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS, with_services=False)
    await setup_balancer()
    events: list[str] = []
    hass.bus.async_listen(
        "power_load_balancer_event", lambda e: events.append(e.data["message"])
    )

    await _drawing(hass, UPSTAIRS, UPSTAIRS_POWER)
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    await clock.advance(600)
    await hold(hass, ARM_LEVEL + 60, 30, clock)

    # The house quietens; the appliance is still ignoring the shed.
    await hold(hass, 2500, 200, clock)

    unresolved = [
        item
        for item in _pending(hass)
        if item["action"] == "attention" and item["entity_id"] == UPSTAIRS
    ]
    assert unresolved, f"forgot an ignored shed once the load fell: {_pending(hass)}"

    ineffective = [m for m in events if "did not take effect" in m]
    assert len(ineffective) == 1, f"warned repeatedly: {len(ineffective)} times"
