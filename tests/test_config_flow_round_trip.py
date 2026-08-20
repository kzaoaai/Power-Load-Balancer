"""
What the options flow saves must be what was typed into it.

The flow rebuilds an appliance's configuration from the fields it knows
about rather than storing the form wholesale, so a field added to the form
but not to that rebuild is accepted, shown as saved, and silently discarded.
Nothing fails and nothing is logged; the setting simply is not there the next
time anyone looks.
"""

from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResultType

from tests.conftest import (
    DOWNSTAIRS,
    DOWNSTAIRS_POWER,
    UPSTAIRS,
    UPSTAIRS_POWER,
)

EDIT_UPSTAIRS = "edit_sensor_0"


async def _open_options(hass, entry):
    """Open the options flow and step into editing the first appliance."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensor_menu"
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": EDIT_UPSTAIRS}
    )


async def _finish(hass, flow_id):
    """Return to the menu and save."""
    return await hass.config_entries.options.async_configure(
        flow_id, {"action": "finish"}
    )


def _offered_defaults(schema) -> dict:
    """Return the values the form pre-fills, as a user resubmitting would."""
    defaults = {}
    for key in schema:
        default = getattr(key, "default", None)
        if not callable(default):
            continue
        value = default()
        if value is not vol.UNDEFINED:
            defaults[str(key)] = value
    return defaults


def _appliance(entry, entity_id: str) -> dict:
    for rung in entry.options["power_sensors"]:
        if rung["appliance"] == entity_id:
            return rung
    message = f"{entity_id} missing from the saved ladder"
    raise AssertionError(message)


async def test_nameplate_survives_the_options_flow(hass, world, setup_balancer):
    """The regression: a nameplate typed in and quietly thrown away."""
    entry = await setup_balancer()

    result = await _open_options(hass, entry)
    assert result["step_id"] == "edit_sensor"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "entity_id": UPSTAIRS_POWER,
            "appliance": UPSTAIRS,
            "name": "Upstairs Tank",
            "importance": 1,
            "last_resort": False,
            "device_cooldown": 180,
            "nominal_power_watt": 2050,
        },
    )
    await _finish(hass, result["flow_id"])
    await hass.async_block_till_done()

    assert _appliance(entry, UPSTAIRS).get("nominal_power_watt") == 2050, (
        "the options flow accepted a nameplate and did not store it"
    )


async def test_a_saved_nameplate_is_shown_when_editing_again(
    hass, world, setup_balancer
):
    """
    A stored value must come back as the field's default, not blank.

    Otherwise the next edit of any other field silently clears it.
    """
    entry = await setup_balancer()
    result = await _open_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "entity_id": UPSTAIRS_POWER,
            "appliance": UPSTAIRS,
            "importance": 1,
            "last_resort": False,
            "device_cooldown": 180,
            "nominal_power_watt": 2050,
        },
    )
    await _finish(hass, result["flow_id"])
    await hass.async_block_till_done()

    result = await _open_options(hass, entry)
    default = _offered_defaults(result["data_schema"].schema)["nominal_power_watt"]
    assert default == 2050, f"editing again offered {default!r}, not the saved value"


async def test_editing_another_field_keeps_the_nameplate(hass, world, setup_balancer):
    """Changing importance must not wipe a nameplate set earlier."""
    entry = await setup_balancer()

    result = await _open_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "entity_id": UPSTAIRS_POWER,
            "appliance": UPSTAIRS,
            "importance": 1,
            "last_resort": False,
            "device_cooldown": 180,
            "nominal_power_watt": 2050,
        },
    )
    await _finish(hass, result["flow_id"])
    await hass.async_block_till_done()

    # Re-open and change only the importance, submitting the offered defaults.
    result = await _open_options(hass, entry)
    submission = _offered_defaults(result["data_schema"].schema)
    submission["importance"] = 3
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submission
    )
    await _finish(hass, result["flow_id"])
    await hass.async_block_till_done()

    rung = _appliance(entry, UPSTAIRS)
    assert rung["importance"] == 3
    assert rung.get("nominal_power_watt") == 2050, "changing importance wiped it"


async def test_zero_is_stored_as_unset(hass, world, setup_balancer):
    """Zero means unknown, so it need not be written out as a value."""
    entry = await setup_balancer()
    result = await _open_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "entity_id": UPSTAIRS_POWER,
            "appliance": UPSTAIRS,
            "importance": 1,
            "last_resort": False,
            "device_cooldown": 180,
            "nominal_power_watt": 0,
        },
    )
    await _finish(hass, result["flow_id"])
    await hass.async_block_till_done()

    assert _appliance(entry, UPSTAIRS).get("nominal_power_watt", 0) == 0


@pytest.mark.parametrize(
    "field",
    ["importance", "last_resort", "device_cooldown", "name", "nominal_power_watt"],
)
async def test_every_offered_field_is_stored(hass, world, setup_balancer, field):
    """
    Guard the whole form, not just the field that caught this out.

    The rebuild that dropped the nameplate would drop any future field the
    same way, so assert that everything the form offers survives a save.
    """
    entry = await setup_balancer()
    result = await _open_options(hass, entry)
    assert field in {str(key) for key in result["data_schema"].schema}, (
        f"{field} is not offered by the edit form"
    )

    values = {
        "entity_id": UPSTAIRS_POWER,
        "appliance": UPSTAIRS,
        "name": "Renamed Tank",
        "importance": 4,
        "last_resort": True,
        "device_cooldown": 240,
        "nominal_power_watt": 1800,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )
    await _finish(hass, result["flow_id"])
    await hass.async_block_till_done()

    rung = _appliance(entry, UPSTAIRS)
    assert rung.get(field) == values[field], (
        f"{field} was accepted by the form but not stored"
    )


async def test_the_other_appliances_are_untouched(hass, world, setup_balancer):
    """Editing one rung must not disturb the rest of the ladder."""
    entry = await setup_balancer()
    result = await _open_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "entity_id": UPSTAIRS_POWER,
            "appliance": UPSTAIRS,
            "importance": 1,
            "last_resort": False,
            "device_cooldown": 180,
            "nominal_power_watt": 2050,
        },
    )
    await _finish(hass, result["flow_id"])
    await hass.async_block_till_done()

    downstairs = _appliance(entry, DOWNSTAIRS)
    assert downstairs["entity_id"] == DOWNSTAIRS_POWER
    assert downstairs["importance"] == 2
    assert len(entry.options["power_sensors"]) == 2
