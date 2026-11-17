"""
The order the balancer sheds in, and the table in the settings that sets it.

Importance alone sets the order: the lowest is shed first. The "last resort"
flag inherited from upstream never shed anything, which made it a stricter
"never shed" than its label promised, and an appliance that should never be
shed does not belong in the balancer. It is gone; a stored flag is ignored.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.data_entry_flow import FlowResultType

from custom_components.power_load_balancer.balancing_engine import shed_order
from tests.conftest import (
    ARM_LEVEL,
    BUDGET,
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
from tests.test_appliance_subentries import _entity_of, _set_balancing
from tests.test_budget_source import BUDGET_ENTITY, _main_input

TRANSLATIONS = (
    Path(__file__).parent.parent
    / "custom_components"
    / "power_load_balancer"
    / "translations"
)


async def test_a_stored_last_resort_flag_no_longer_exempts_an_appliance(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Saved with the old flag, the least important tank is still shed first."""
    rungs = ladder()
    rungs[0]["last_resort"] = True
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer(power_sensors=rungs)

    for tank, sensor in ((UPSTAIRS, UPSTAIRS_POWER), (DOWNSTAIRS, DOWNSTAIRS_POWER)):
        hass.states.async_set(tank, "electric", {"operation_list": ["electric", "off"]})
        hass.states.async_set(sensor, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hold(hass, ARM_LEVEL + 60, 75, clock)

    sheds = [entity for _, service, entity in world.calls if service == "shed"]
    assert sheds[:1] == [UPSTAIRS], world.calls


async def test_the_settings_list_appliances_in_shed_order(hass, world, setup_balancer):
    """Lowest importance first, ties by when added, appliances put away last."""
    rungs = ladder(include_heater=True)
    rungs[2]["importance"] = 1
    rungs[2]["name"] = "Spare Heater | Garage"
    entry = await setup_balancer(power_sensors=rungs)
    switch = _entity_of(hass, entry, DOWNSTAIRS, "switch", "appliance_balancing")
    await _set_balancing(hass, switch, on=False)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "appliances" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "appliances"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "appliances"
    assert result["description_placeholders"]["rows"].splitlines() == [
        "| 1 | Upstairs Tank | 1 |",
        "| 2 | Spare Heater \\| Garage | 1 |",
        "| - | Downstairs Tank | 2 |",
    ]

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    hass.config_entries.options.async_abort(result["flow_id"])


def test_every_language_has_the_table_and_no_last_resort() -> None:
    """The header lives in the translation, so each language must carry it."""
    for path in TRANSLATIONS.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        strings = json.loads(text)
        step = strings["options"]["step"]
        assert "appliances" in step["init"]["menu_options"], path.name
        assert "{rows}" in step["appliances"]["description"], path.name
        assert "|:-:|:--|:-:|" in step["appliances"]["description"], path.name
        assert "last_resort" not in text, path.name


def _count_reloads(hass, monkeypatch) -> list[str]:
    """Record every reload of a config entry, letting it run."""
    reloads: list[str] = []
    reload = hass.config_entries.async_reload

    async def _counting_reload(entry_id: str) -> bool:
        reloads.append(entry_id)
        return await reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _counting_reload)
    return reloads


async def _table(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "appliances"}
    )


async def _save(hass, result) -> None:
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def _importances(entry) -> dict[str, int]:
    return {sub.title: sub.data["importance"] for sub in entry.subentries.values()}


async def test_moving_a_slider_reorders_the_table_before_anything_is_saved(
    hass, world, setup_balancer
):
    """The table re-sorts on submit; the stored importances do not move yet."""
    entry = await setup_balancer(power_sensors=ladder(include_heater=True))
    result = await _table(hass, entry)
    assert [str(key) for key in result["data_schema"].schema] == [
        "Upstairs Tank",
        "Downstairs Tank",
        "Spare Heater",
    ]

    moved = {"Upstairs Tank": 3, "Downstairs Tank": 2, "Spare Heater": 1}
    result = await hass.config_entries.options.async_configure(result["flow_id"], moved)
    assert result["step_id"] == "appliances", "a moved slider left the table"
    assert result["description_placeholders"]["rows"].splitlines() == [
        "| 1 | Spare Heater | 1 |",
        "| 2 | Downstairs Tank | 2 |",
        "| 3 | Upstairs Tank | 3 |",
    ]
    assert _importances(entry)["Upstairs Tank"] == 1, "saved before Save"

    result = await hass.config_entries.options.async_configure(result["flow_id"], moved)
    assert result["type"] is FlowResultType.MENU
    hass.config_entries.options.async_abort(result["flow_id"])
    assert _importances(entry)["Upstairs Tank"] == 1, "kept without Save"


async def test_save_writes_every_moved_importance_with_one_reload(
    hass, world, setup_balancer, monkeypatch
):
    """Three subentries and the settings change; the balancer reloads once."""
    entry = await setup_balancer(power_sensors=ladder(include_heater=True))
    reloads = _count_reloads(hass, monkeypatch)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_main_sensor"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _main_input(cooldown_seconds=30)
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "appliances"}
    )
    moved = {"Upstairs Tank": 3, "Downstairs Tank": 2, "Spare Heater": 1}
    result = await hass.config_entries.options.async_configure(result["flow_id"], moved)
    result = await hass.config_entries.options.async_configure(result["flow_id"], moved)
    await _save(hass, result)

    assert reloads == [entry.entry_id], reloads
    assert _importances(entry) == moved
    assert entry.data["cooldown_seconds"] == 30
    balancer = balancer_of(hass, entry)
    assert [s["appliance"] for s in shed_order(balancer._monitored_sensors)] == [
        HEATER,
        DOWNSTAIRS,
        UPSTAIRS,
    ]


async def test_a_save_that_changes_nothing_does_not_reload(
    hass, world, setup_balancer, monkeypatch
):
    """Opening the table and saving as it was is not a change."""
    entry = await setup_balancer()
    reloads = _count_reloads(hass, monkeypatch)
    result = await _table(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await _save(hass, result)
    assert reloads == []


async def test_a_budget_already_stored_leaves_no_reload_skip_behind(
    hass, world, setup_balancer, monkeypatch
):
    """
    The budget number arms a skip for its own write.

    A value already stored (a variable budget had moved the running one away
    from it) wrote nothing, so no listener consumed the skip, and the next
    write that relies on its listener went without its reload: here, putting
    an appliance away with its balancing switch.
    """
    entry = await setup_balancer()
    balancer_of(hass, entry).set_power_budget(5000)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": BUDGET_ENTITY, "value": BUDGET},
        blocking=True,
    )
    await hass.async_block_till_done()

    reloads = _count_reloads(hass, monkeypatch)
    switch = _entity_of(hass, entry, DOWNSTAIRS, "switch", "appliance_balancing")
    await _set_balancing(hass, switch, on=False)
    assert reloads == [entry.entry_id], "the write was swallowed by a stale skip"
