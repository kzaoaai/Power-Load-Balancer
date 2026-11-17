"""
The order the balancer sheds in, and the table in the settings that shows it.

Importance alone sets the order: the lowest is shed first. The "last resort"
flag inherited from upstream never shed anything, which made it a stricter
"never shed" than its label promised, and an appliance that should never be
shed does not belong in the balancer. It is gone; a stored flag is ignored.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.data_entry_flow import FlowResultType

from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    DOWNSTAIRS_POWER,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    hold,
    ladder,
)
from tests.test_appliance_subentries import _entity_of, _set_balancing

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
