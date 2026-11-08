"""
The budget follows the supply carrying the house.

An automation used to push the budget on a schedule and on source changes.
The balancer now follows the source sensor itself: a lower budget applies at
once, because the new supply may already be overloaded; a higher one waits,
so a supply that flickers back for a moment does not restore loads onto it.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.power_load_balancer.config_flow import _parse_budget_map
from tests.conftest import balancer_of

SOURCE = "sensor.home_power_source"
BUDGET_ENTITY = "number.power_load_balancer_power_budget"
SOURCE_MAP = {"Solar": 8000, "Battery": 8000, "Utility": 8200, "Generator": 4400}


async def _setup(hass, setup_balancer, source_state: str):
    hass.states.async_set(SOURCE, source_state)
    return await setup_balancer(
        budget_source_entity=SOURCE,
        budget_source_map=SOURCE_MAP,
        budget_raise_delay_seconds=60,
    )


def _budget(hass) -> float:
    return float(hass.states.get(BUDGET_ENTITY).state)


async def test_the_supply_in_force_at_startup_sets_the_budget(
    hass, world, setup_balancer
):
    """A restart on the generator must not run on the last solar budget."""
    entry = await _setup(hass, setup_balancer, "Generator")
    assert balancer_of(hass, entry).power_budget == 4400
    assert _budget(hass) == 4400


async def test_a_lower_budget_applies_at_once_and_a_higher_one_waits(
    hass, world, setup_balancer
):
    """Down immediately, up only once the better supply has held."""
    entry = await _setup(hass, setup_balancer, "Solar")
    balancer = balancer_of(hass, entry)
    assert balancer.power_budget == 8000

    hass.states.async_set(SOURCE, "Generator")
    await hass.async_block_till_done()
    assert balancer.power_budget == 4400
    assert _budget(hass) == 4400

    hass.states.async_set(SOURCE, "Solar")
    await hass.async_block_till_done()
    assert balancer.power_budget == 4400, "raised before the delay"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert balancer.power_budget == 8000
    assert _budget(hass) == 8000


async def test_a_supply_that_flickers_back_does_not_raise_the_budget(
    hass, world, setup_balancer
):
    """Back to the generator within the delay cancels the pending rise."""
    entry = await _setup(hass, setup_balancer, "Generator")
    balancer = balancer_of(hass, entry)

    hass.states.async_set(SOURCE, "Solar")
    await hass.async_block_till_done()
    hass.states.async_set(SOURCE, "Generator")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert balancer.power_budget == 4400


async def test_an_unmapped_state_keeps_the_budget(hass, world, setup_balancer):
    """Unavailable or an unknown supply is not a reason to change anything."""
    entry = await _setup(hass, setup_balancer, "Generator")
    hass.states.async_set(SOURCE, "unavailable")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert balancer_of(hass, entry).power_budget == 4400


def test_the_budget_map_parser() -> None:
    """One `state = watts` per line; anything else is refused whole."""
    assert _parse_budget_map("Generator = 4400\n\nSolar=8000\n") == {
        "Generator": 4400,
        "Solar": 8000,
    }
    assert _parse_budget_map("") == {}
    for bad in ("Generator", "Generator = lots", "Generator = 0", "= 4400"):
        assert _parse_budget_map(bad) is None, bad


async def test_the_options_flow_stores_the_source_settings(hass, world, setup_balancer):
    """What the form takes is what the balancer is reloaded with."""
    entry = await setup_balancer()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_budget_source"}
    )
    assert result["step_id"] == "edit_budget_source"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "budget_source_entity": SOURCE,
            "budget_source_map": "Generator = 4400\nSolar = 8000",
            "budget_raise_delay_seconds": 30,
        },
    )
    assert result["step_id"] == "sensor_menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.data["budget_source_entity"] == SOURCE
    assert entry.data["budget_source_map"] == {"Generator": 4400, "Solar": 8000}
    assert entry.data["budget_raise_delay_seconds"] == 30


async def test_the_options_flow_refuses_a_malformed_map(hass, world, setup_balancer):
    """A typo must be shown, not saved as an empty map."""
    entry = await setup_balancer()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_budget_source"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"budget_source_entity": SOURCE, "budget_source_map": "Generator 4400"},
    )
    assert result["errors"] == {"budget_source_map": "invalid_budget_map"}
