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

from custom_components.power_load_balancer import config_flow
from tests.conftest import BUDGET, MAIN_SENSOR, balancer_of

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


async def _main_form(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_main_sensor"}
    )


def _main_input(**changes: object) -> dict:
    return {
        "main_power_sensor": MAIN_SENSOR,
        "power_budget_watt": BUDGET,
        "cooldown_seconds": 10,
        **changes,
    }


async def _pick(hass, result, entity_id: str = SOURCE):
    """Choose the source on the variable budget page, which lists its states."""
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"budget_source_entity": entity_id}
    )


def _offered(result) -> list[str]:
    return [str(key) for key in result["data_schema"].schema]


async def _finish(hass, result):
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_a_variable_budget_is_set_per_state_in_the_main_settings(
    hass, world, setup_balancer
):
    """Pick variable, then the entity and one field per state it takes."""
    hass.states.async_set(
        SOURCE, "Solar", {"options": ["Solar", "Battery", "Utility", "Generator"]}
    )
    entry = await setup_balancer()
    result = await _main_form(hass, entry)
    assert "budget_mode" in _offered(result)

    assert "budget_source_entity" not in _offered(result)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _main_input(budget_mode="source")
    )
    assert result["step_id"] == "budget_states"
    assert _offered(result) == ["budget_source_entity", "budget_raise_delay_seconds"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"budget_source_entity": SOURCE, "budget_raise_delay_seconds": 30},
    )
    assert result["step_id"] == "budget_states"
    assert _offered(result) == [
        "budget_source_entity",
        "budget_raise_delay_seconds",
        "Solar",
        "Battery",
        "Utility",
        "Generator",
        "add_states",
    ]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "budget_source_entity": SOURCE,
            "budget_raise_delay_seconds": 30,
            "Generator": 4400,
            "Utility": 8200.0,
            "add_states": [],
        },
    )
    assert result["step_id"] == "init"
    await _finish(hass, result)

    assert entry.data["budget_source_entity"] == SOURCE
    assert entry.data["budget_source_map"] == {"Generator": 4400, "Utility": 8200}
    assert entry.data["budget_raise_delay_seconds"] == 30


async def test_a_state_the_entity_does_not_declare_can_be_added(
    hass, world, setup_balancer
):
    """A plain sensor lists no options; typing a state gives it a field."""
    hass.states.async_set(SOURCE, "Solar")
    entry = await setup_balancer()
    result = await _main_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _main_input(budget_mode="source")
    )
    result = await _pick(hass, result)
    assert _offered(result)[2:] == ["Solar", "add_states"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"budget_source_entity": SOURCE, "Solar": 8000, "add_states": ["Generator"]},
    )
    assert result["step_id"] == "budget_states"
    assert _offered(result)[2:] == ["Solar", "Generator", "add_states"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"budget_source_entity": SOURCE, "Solar": 8000, "Generator": 4400},
    )
    await _finish(hass, result)
    assert entry.data["budget_source_map"] == {"Solar": 8000, "Generator": 4400}


async def test_the_per_state_form_needs_one_budget(hass, world, setup_balancer):
    """All blank would follow the entity and never change anything."""
    hass.states.async_set(SOURCE, "Solar")
    entry = await setup_balancer()
    result = await _main_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _main_input(budget_mode="source")
    )
    result = await _pick(hass, result)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"budget_source_entity": SOURCE}
    )
    assert result["errors"] == {"base": "budget_map_required"}


async def test_a_variable_budget_needs_an_entity(hass, world, setup_balancer):
    """Variable with nothing to follow is refused on its own page."""
    entry = await setup_balancer()
    result = await _main_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _main_input(budget_mode="source")
    )
    assert result["step_id"] == "budget_states"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "budget_states"
    assert result["errors"] == {"budget_source_entity": "source_entity_required"}


async def test_a_different_source_lists_its_own_states_afresh(
    hass, world, setup_balancer
):
    """Changing the entity on the page re-lists states and drops old budgets."""
    other = "select.other_source"
    hass.states.async_set(SOURCE, "Generator")
    hass.states.async_set(other, "Day", {"options": ["Day", "Night"]})
    entry = await _setup(hass, setup_balancer, "Generator")
    result = await _main_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _main_input(budget_mode="source")
    )
    assert result["step_id"] == "budget_states"
    assert set(SOURCE_MAP) <= set(_offered(result)), "saved states not offered"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"budget_source_entity": other, "Generator": 4400}
    )
    assert result["step_id"] == "budget_states"
    assert _offered(result)[2:] == ["Day", "Night", "add_states"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"budget_source_entity": other, "Night": 3000}
    )
    await _finish(hass, result)
    assert entry.data["budget_source_entity"] == other
    assert entry.data["budget_source_map"] == {"Night": 3000}


async def test_switching_to_fixed_stops_following_but_keeps_the_states(
    hass, world, setup_balancer
):
    """Fixed turns the feature off; switching back offers the old budgets."""
    hass.states.async_set(SOURCE, "Generator")
    entry = await _setup(hass, setup_balancer, "Generator")
    result = await _main_form(hass, entry)
    mode = next(k for k in result["data_schema"].schema if str(k) == "budget_mode")
    assert mode.default() == "source", "a following budget opened as fixed"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _main_input(budget_mode="fixed")
    )
    assert result["step_id"] == "init"
    await _finish(hass, result)

    assert "budget_source_entity" not in entry.data
    assert entry.data["budget_source_map"] == SOURCE_MAP
    assert balancer_of(hass, entry).power_budget == BUDGET


async def test_states_are_offered_from_options_history_and_now(hass, monkeypatch):
    """Declared first, then seen, then current, then saved; junk dropped."""
    hass.states.async_set(SOURCE, "Utility", {"options": ["Solar", "Battery"]})

    async def _seen(_hass, _entity):
        return ["Generator", "unavailable", "Solar", "unknown"]

    monkeypatch.setattr(config_flow, "_recorded_states", _seen)
    states = await config_flow._known_source_states(hass, SOURCE, ["Grid", "Solar"])
    assert states == ["Solar", "Battery", "Generator", "Utility", "Grid"]
