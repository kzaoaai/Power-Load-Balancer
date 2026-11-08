"""
What an appliance draws, when its own sensor cannot say.

A shed appliance reads zero, and a sensor that polls once a minute reads zero
for a while after one starts. A nameplate answered that before, but only when
someone knew it and typed it in. The balancer now learns the figure from what
the sensor measured while the appliance ran, and a fixed value stays
available for anyone who prefers one.
"""

from __future__ import annotations

from homeassistant.data_entry_flow import FlowResultType

from custom_components.power_load_balancer.const import (
    LEARN_MIN_SAMPLES,
    LEARN_WINDOW_SAMPLES,
)
from custom_components.power_load_balancer.draw_learning import (
    DrawLearner,
    resolve_power_draw_mode,
)
from tests.conftest import (
    ARM_LEVEL,
    UPSTAIRS,
    UPSTAIRS_POWER,
    balancer_of,
    hold,
    ladder,
)

LOW, HIGH = 1450, 1870
# A tank's meter wanders a few watts; repeated identical readings are no
# state change at all, so each level is fed as a run of distinct values.
_TWO_LEVELS = [LOW + index % 7 for index in range(30)] + [HIGH + i for i in range(5)]


def test_a_two_level_draw_is_learned_at_its_upper_level() -> None:
    """A tank that steps up late in its cycle must be sized on the step."""
    learner = DrawLearner()
    for index in range(100):
        learner.add(UPSTAIRS, HIGH if index % 10 < 3 else LOW)
    assert learner.learned(UPSTAIRS) == HIGH


def test_too_few_readings_and_standby_teach_nothing() -> None:
    """Unknown stays unknown until there is enough to go on."""
    learner = DrawLearner()
    for _ in range(LEARN_MIN_SAMPLES - 1):
        learner.add(UPSTAIRS, LOW)
    assert learner.learned(UPSTAIRS) == 0
    assert not learner.add(UPSTAIRS, 3), "a standby reading was learned"
    assert not learner.add(UPSTAIRS, float("nan"))
    learner.add(UPSTAIRS, LOW)
    assert learner.learned(UPSTAIRS) == LOW


def test_only_recent_readings_count() -> None:
    """A selector moved to a higher setting shows up once the window turns."""
    learner = DrawLearner()
    for _ in range(LEARN_WINDOW_SAMPLES):
        learner.add(UPSTAIRS, 525)
    for _ in range(LEARN_WINDOW_SAMPLES):
        learner.add(UPSTAIRS, 1300)
    assert learner.learned(UPSTAIRS) == 1300
    assert learner.sample_count(UPSTAIRS) == LEARN_WINDOW_SAMPLES


def test_an_upgrade_keeps_each_appliance_as_it_was() -> None:
    """A nameplate set before the choice existed stays fixed; none means learn."""
    assert resolve_power_draw_mode({"nominal_power_watt": 2000}) == "fixed"
    assert resolve_power_draw_mode({"nominal_power_watt": 0}) == "learn"
    assert resolve_power_draw_mode({}) == "learn"
    assert (
        resolve_power_draw_mode(
            {"nominal_power_watt": 2000, "power_draw_mode": "learn"}
        )
        == "learn"
    )


async def _teach(hass, clock, levels: list[int]) -> None:
    for watts in levels:
        hass.states.async_set(UPSTAIRS_POWER, str(watts), {"unit_of_measurement": "W"})
        await hass.async_block_till_done()
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()


async def _turn_on_by_hand(hass) -> None:
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    await hass.async_block_till_done()


async def test_the_veto_sizes_on_the_learned_draw(hass, world, clock, setup_balancer):
    """A tank turned on into too little headroom is caught before it draws."""
    entry = await setup_balancer()
    await _teach(hass, clock, _TWO_LEVELS)
    controller = balancer_of(hass, entry)._appliance_controller
    assert controller.get_reference_power(UPSTAIRS) >= HIGH

    await hold(hass, ARM_LEVEL - 1000, 10, clock)
    await _turn_on_by_hand(hass)

    assert world.of(UPSTAIRS), "let a tank start that its learned draw cannot fit"
    log = hass.states.get("sensor.power_load_balancer_log").attributes["events"]
    assert any("plus 187" in line for line in log), log


async def test_a_fixed_draw_wins_over_what_was_learned(
    hass, world, clock, setup_balancer
):
    """Fixed means fixed, whatever the sensor has seen."""
    rungs = ladder(upstairs_nameplate=900)
    rungs[0]["power_draw_mode"] = "fixed"
    entry = await setup_balancer(power_sensors=rungs)
    await _teach(hass, clock, [HIGH + index for index in range(30)])
    controller = balancer_of(hass, entry)._appliance_controller
    assert controller.get_reference_power(UPSTAIRS) == 900

    await hold(hass, ARM_LEVEL - 1000, 10, clock)
    await _turn_on_by_hand(hass)
    assert world.of(UPSTAIRS) == [], "vetoed on the learned draw in fixed mode"


async def test_a_shed_on_the_lower_level_is_restored_on_the_upper(
    hass, world, clock, setup_balancer
):
    """What restoring costs is the learned draw when the shed caught less."""
    entry = await setup_balancer()
    await _teach(hass, clock, _TWO_LEVELS)
    balancer = balancer_of(hass, entry)
    hass.states.async_set(UPSTAIRS_POWER, str(LOW), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()

    assert balancer._restore_size(UPSTAIRS) >= HIGH


async def test_what_was_learned_survives_a_reload(hass, world, clock, setup_balancer):
    """Learning a tank takes days; a restart must not start it over."""
    entry = await setup_balancer()
    await _teach(hass, clock, [LOW + index for index in range(30)])
    learned = balancer_of(hass, entry)._appliance_controller.get_reference_power(
        UPSTAIRS
    )
    assert learned > 0

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    controller = balancer_of(hass, entry)._appliance_controller
    assert controller.get_reference_power(UPSTAIRS) == learned


async def _add_sensor_form(hass, entry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, "appliance"), context={"source": "user"}
    )


def _new_rung(entry) -> dict:
    return next(
        dict(sub.data)
        for sub in entry.subentries.values()
        if sub.data["appliance"] == NEW_RUNG["appliance"]
    )


NEW_RUNG = {
    "entity_id": "sensor.spare_heater_power",
    "appliance": "switch.spare_heater",
    "importance": 3,
    "last_resort": False,
}


async def test_a_new_appliance_learns_by_default(hass, world, setup_balancer):
    """Nothing to type: leaving the defaults means learning."""
    entry = await setup_balancer()
    result = await _add_sensor_form(hass, entry)
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], dict(NEW_RUNG)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _new_rung(entry)["power_draw_mode"] == "learn"


async def test_a_fixed_draw_needs_a_value(hass, world, setup_balancer):
    """Fixed with no watts would silently mean unknown; the form says so."""
    entry = await setup_balancer()
    result = await _add_sensor_form(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**NEW_RUNG, "power_draw_mode": "fixed"}
    )
    assert result["errors"] == {"nominal_power_watt": "fixed_power_required"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {**NEW_RUNG, "power_draw_mode": "fixed", "nominal_power_watt": 1500},
    )
    await hass.async_block_till_done()
    rung = _new_rung(entry)
    assert rung["power_draw_mode"] == "fixed"
    assert rung["nominal_power_watt"] == 1500
