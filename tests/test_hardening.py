"""
Smaller ways the balancer could leave an appliance or its own records wrong.

Each test pins one fix from the November 2026 review: an appliance that only
reappeared being vetoed, a tank shed one way and restored another, a restore
that did nothing being forgotten, a NaN meter reading, a log message too long
for a state, a Repairs notice nothing ever withdrew, a meter gone silent, and a
timer restore that cancelled itself.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.power_load_balancer import power_balancer
from custom_components.power_load_balancer.const import (
    DOMAIN,
    appliance_control_issue_id,
)
from custom_components.power_load_balancer.exceptions import PowerSensorError
from custom_components.power_load_balancer.validation import validate_power_value
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

LOG_SENSOR = "sensor.power_load_balancer_log"


async def test_an_appliance_coming_back_from_unavailable_is_not_vetoed(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A tank that drops off the network while heating was never switched on.

    Its draw is already in the meter total; vetoing it as a fresh turn-on
    counts that draw twice and switches off a running tank.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer(power_sensors=ladder(upstairs_nameplate=TANK_VA))
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    await hold(hass, 5000, 20, clock)
    world.clear()

    hass.states.async_set(UPSTAIRS, "unavailable", {})
    await hass.async_block_till_done()
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    await hass.async_block_till_done()

    assert world.of(UPSTAIRS) == [], "switched off a tank that only reconnected"


async def test_a_tank_shed_by_mode_is_restored_by_mode(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A tank shed before its shed service existed must not be "released".

    Shed through set_operation_mode, it sits in "off"; a release only clears a
    shed flag it never had, and the tank would stay off for good.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS, with_services=False)
    await setup_balancer()
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert world.services_for(UPSTAIRS) == ["set_operation_mode"]
    hass.states.async_set(UPSTAIRS, "off", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})

    # The shed service registers only now, as it would late in startup.
    world.register(hass, "generic_water_heater", "shed")
    world.register(hass, "generic_water_heater", "release")
    await hold(hass, 2000, 400, clock)

    assert world.services_for(UPSTAIRS) == [
        "set_operation_mode",
        "set_operation_mode",
    ], f"restored the wrong way: {world.services_for(UPSTAIRS)}"


async def test_a_restore_that_could_do_nothing_keeps_holding_the_appliance(
    hass, world, clock, setup_balancer
):
    """An appliance nothing could turn back on must not drop off the books."""
    radiator = "climate.spare_radiator"
    radiator_power = "sensor.spare_radiator_power"
    modes: list[str] = []

    async def _set_hvac_mode(call: ServiceCall) -> None:
        modes.append(call.data["hvac_mode"])

    hass.services.async_register("climate", "set_hvac_mode", _set_hvac_mode)
    hass.states.async_set(radiator, "heat", {"hvac_modes": ["off", "heat"]})
    hass.states.async_set(radiator_power, "1500", {"unit_of_measurement": "W"})
    rung = {
        "appliance": radiator,
        "entity_id": radiator_power,
        "name": "Spare Radiator",
        "importance": 3,
        "last_resort": False,
    }
    entry = await setup_balancer(power_sensors=[*ladder(), rung])
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    assert modes == ["off"]

    # The radiator now offers no mode to come back to.
    hass.states.async_set(radiator, "off", {"hvac_modes": ["off"]})
    hass.states.async_set(radiator_power, "0", {"unit_of_measurement": "W"})
    controller = balancer_of(hass, entry)._appliance_controller
    controller.clear_previous_hvac_mode(radiator)
    await hold(hass, 1500, 400, clock)

    assert modes == ["off"]
    assert radiator in controller.get_balanced_off_appliances(), (
        "forgot a radiator it could not turn back on"
    )


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_a_reading_that_is_not_a_finite_number_is_rejected(value: str) -> None:
    """NaN compares false with everything, so it would freeze all decisions."""
    with pytest.raises(PowerSensorError):
        validate_power_value(value, "sensor.meter")


async def test_a_long_message_does_not_wipe_the_log(hass, world, setup_balancer):
    """A state over 255 characters is rejected, and the log went with it."""
    entry = await setup_balancer()
    sensor = balancer_of(hass, entry)._event_log_sensor
    sensor.add_log_entry("first entry")
    sensor.add_log_entry("x" * 400)
    await hass.async_block_till_done()

    state = hass.states.get(LOG_SENSOR)
    assert len(state.state) == 255
    assert len(state.attributes["events"]) >= 2, "the log was dropped"


async def test_an_appliance_control_notice_is_withdrawn_once_usable(
    hass, world, clock, setup_balancer
):
    """The notice raised when a call finds an appliance gone must not linger."""
    entry = await setup_balancer()
    ir.async_create_issue(
        hass,
        DOMAIN,
        appliance_control_issue_id(UPSTAIRS),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="device_unavailable",
        translation_placeholders={"entity_id": UPSTAIRS},
    )
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, appliance_control_issue_id(UPSTAIRS))

    balancer_of(hass, entry)._evaluate_entity_health()

    assert not registry.async_get_issue(DOMAIN, appliance_control_issue_id(UPSTAIRS))


async def test_a_silent_meter_pauses_balancing_until_it_reports(  # noqa: PLR0913, PLR0917
    hass, world, clock, setup_balancer, register_shed_platform, monkeypatch
):
    """
    A meter that stops updating but keeps its number must not be acted on.

    Its last reading looks valid to every other check, so the dwell would
    elapse against a house nobody can see any more and shed a tank for it.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 20, clock)
    world.clear()

    # The meter goes quiet for six minutes, still showing an overload.
    silent_since = time.time()
    monkeypatch.setattr(power_balancer, "wall_time", lambda: silent_since + 360)
    await clock.advance(360)
    balancer = balancer_of(hass, entry)
    balancer.async_check_and_balance()
    await hass.async_block_till_done()

    assert world.of(UPSTAIRS) == [], "shed a tank on a reading six minutes old"
    log = hass.states.get(LOG_SENSOR).attributes["events"]
    assert any("has not reported" in line for line in log), log

    # It reports again; balancing resumes and says so.
    monkeypatch.setattr(power_balancer, "wall_time", time.time)
    await hold(hass, 3000, 20, clock)
    log = hass.states.get(LOG_SENSOR).attributes["events"]
    assert any("reporting again" in line for line in log), log


async def test_a_timer_restore_does_not_cancel_itself(
    hass, world, clock, setup_balancer
):
    """The state change a timer restore causes must not cancel that timer."""
    entry = await setup_balancer()
    balancer = balancer_of(hass, entry)
    controller = balancer._appliance_controller

    async def _pending() -> None:
        await asyncio.sleep(3600)

    for own, expect_cancelled in ((True, False), (False, True)):
        task = hass.async_create_background_task(_pending(), "test timer")
        controller._scheduled_auto_turn_ons[UPSTAIRS] = task
        if own:
            controller.note_own_restore(UPSTAIRS)
        await balancer._handle_appliance_turn_on(UPSTAIRS)
        await asyncio.sleep(0)
        assert task.cancelled() is expect_cancelled, (own, task)
        task.cancel()
        controller._scheduled_auto_turn_ons.pop(UPSTAIRS, None)
