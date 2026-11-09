"""
Each appliance is a subentry, with its own entities on its own device.

The appliances used to be a list edited one at a time behind a dropdown, and
an appliance put away for the season stayed in it, raising repairs all
summer. Now each is a subentry with a balancing switch, and its entities sit
on the device page of the appliance itself.
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_load_balancer.const import DOMAIN
from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    DOWNSTAIRS_POWER,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    balancer_of,
    hold,
    ladder,
)
from tests.test_entity_health_repairs import GHOST, _settle


def _subentry(entry, appliance: str):
    return next(
        sub for sub in entry.subentries.values() if sub.data["appliance"] == appliance
    )


def _entity_of(hass, entry, appliance: str, platform: str, key: str) -> str:
    unique_id = f"{entry.entry_id}_{_subentry(entry, appliance).subentry_id}_{key}"
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"no {key} entity for {appliance}"
    return entity_id


def _register_on_device(hass, entity_id: str, domain: str, name: str):
    """Give an entity a device owned by another integration's entry."""
    owner = MockConfigEntry(domain=domain)
    owner.add_to_hass(hass)
    hass.data.setdefault("_owners", {})[entity_id] = owner.entry_id
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={(domain, entity_id)},
        name=name,
    )
    existing = hass.states.get(entity_id)
    if existing is not None:
        hass.states.async_remove(entity_id)
    platform, object_id = entity_id.split(".", 1)
    er.async_get(hass).async_get_or_create(
        platform,
        domain,
        f"{domain}_{object_id}",
        suggested_object_id=object_id,
        config_entry=owner,
        device_id=device.id,
    )
    if existing is not None:
        hass.states.async_set(entity_id, existing.state, dict(existing.attributes))
    return device


async def _set_balancing(hass, entity_id: str, *, on: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_an_old_ladder_becomes_one_subentry_per_appliance(
    hass, world, setup_balancer
):
    """Every appliance comes across, balanced, keyed on its entity."""
    entry = await setup_balancer()

    subentries = list(entry.subentries.values())
    assert {sub.unique_id for sub in subentries} == {UPSTAIRS, DOWNSTAIRS}
    assert all(sub.subentry_type == "appliance" for sub in subentries)
    assert all(sub.data["balancing_enabled"] for sub in subentries)
    assert _subentry(entry, UPSTAIRS).title == "Upstairs Tank"


async def test_its_entities_sit_on_the_appliance_device(hass, world, setup_balancer):
    """Shown on the heater's own page, without claiming the heater's device."""
    device = _register_on_device(hass, UPSTAIRS, "generic_water_heater", "Upstairs")
    entry = await setup_balancer()

    registry = er.async_get(hass)
    for platform, key in (
        ("switch", "appliance_balancing"),
        ("sensor", "learned_draw"),
        ("binary_sensor", "shed_by_balancer"),
    ):
        entity = registry.async_get(_entity_of(hass, entry, UPSTAIRS, platform, key))
        assert entity.device_id == device.id, f"{key} is not on the heater's device"
        assert entity.config_subentry_id == _subentry(entry, UPSTAIRS).subentry_id

    linked = dr.async_get(hass).async_get(device.id)
    assert linked.config_entry_id == hass.data["_owners"][UPSTAIRS], (
        "the balancer claimed the heater's device as its own"
    )
    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    assert hass.states.get(switch).name == "Upstairs PLB enable"


async def test_its_entities_say_plb_and_its_sensors_are_diagnostic(
    hass, world, setup_balancer
):
    """On another integration's page, each must say it is the balancer's."""
    _register_on_device(hass, UPSTAIRS, "generic_water_heater", "Upstairs")
    entry = await setup_balancer()

    registry = er.async_get(hass)
    expected = {
        ("switch", "appliance_balancing"): ("Upstairs PLB enable", None),
        ("sensor", "learned_draw"): ("Upstairs PLB learned draw", "diagnostic"),
        ("binary_sensor", "shed_by_balancer"): ("Upstairs PLB shed", "diagnostic"),
    }
    for (platform, key), (name, category) in expected.items():
        entity_id = _entity_of(hass, entry, UPSTAIRS, platform, key)
        assert hass.states.get(entity_id).name == name
        found = registry.async_get(entity_id).entity_category
        assert (found.value if found else None) == category, f"{key} is {found}"


async def test_a_plug_meter_device_is_used_when_the_appliance_has_none(
    hass, world, setup_balancer
):
    """A template switch has no device; the plug measuring it does."""
    plug = _register_on_device(hass, DOWNSTAIRS_POWER, "tuya", "Downstairs plug")
    entry = await setup_balancer()

    switch = _entity_of(hass, entry, DOWNSTAIRS, "switch", "appliance_balancing")
    assert er.async_get(hass).async_get(switch).device_id == plug.id


async def test_with_no_device_at_all_the_entities_carry_the_appliance_name(
    hass, world, setup_balancer
):
    """Nothing to sit on: the name must still say which appliance it is."""
    entry = await setup_balancer()

    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    assert er.async_get(hass).async_get(switch).device_id is None
    assert hass.states.get(switch).name == "Upstairs Tank PLB enable"


async def test_an_appliance_switched_off_is_never_shed(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Put away for the season: out of the ladder, settings kept."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    await _set_balancing(hass, switch, on=False)

    assert hass.states.get(switch).state == "off"
    assert _subentry(entry, UPSTAIRS).data["balancing_enabled"] is False
    assert not balancer_of(hass, entry).is_balanced(UPSTAIRS)

    for tank, sensor in ((UPSTAIRS, UPSTAIRS_POWER), (DOWNSTAIRS, DOWNSTAIRS_POWER)):
        hass.states.async_set(tank, "electric", {"operation_list": ["electric", "off"]})
        hass.states.async_set(sensor, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    await hold(hass, ARM_LEVEL + 60, 30, clock)
    assert world.services_for(UPSTAIRS) == [], "shed an appliance put away"
    assert world.services_for(DOWNSTAIRS) == ["shed"]

    await _set_balancing(hass, switch, on=True)
    assert balancer_of(hass, entry).is_balanced(UPSTAIRS)


async def test_switching_off_gives_back_an_appliance_held_off(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Once left out nothing would ever restore it, so it goes back first."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await setup_balancer()
    for tank, sensor in ((UPSTAIRS, UPSTAIRS_POWER), (DOWNSTAIRS, DOWNSTAIRS_POWER)):
        hass.states.async_set(tank, "electric", {"operation_list": ["electric", "off"]})
        hass.states.async_set(sensor, str(TANK_VA), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hold(hass, ARM_LEVEL + 60, 75, clock)
    await hold(hass, ARM_LEVEL + 60, 30, clock)
    assert world.services_for(UPSTAIRS) == ["shed"]

    shed = _entity_of(hass, entry, UPSTAIRS, "binary_sensor", "shed_by_balancer")
    assert hass.states.get(shed).state == "on"
    assert hass.states.get(shed).attributes["reason"]

    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    await _set_balancing(hass, switch, on=False)

    assert world.services_for(UPSTAIRS) == ["shed", "release"]
    assert hass.states.get(shed).state == "off"


async def test_switching_off_clears_its_repairs(hass, world, clock, setup_balancer):
    """A repair raised before it was put away would otherwise stay forever."""
    rungs = ladder()
    rungs[0]["entity_id"] = GHOST
    entry = await setup_balancer(power_sensors=rungs)
    await _settle(hass, clock)

    def _issues() -> set[str]:
        return {i for (d, i) in ir.async_get(hass).issues if d == DOMAIN}

    assert _issues(), "the missing sensor raised nothing to clear"
    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    await _set_balancing(hass, switch, on=False)
    await _settle(hass, clock)

    assert not _issues(), f"repairs left for an appliance put away: {_issues()}"


async def test_switching_off_keeps_what_was_learned(hass, world, setup_balancer):
    """Learning a heater takes weeks; a summer off must not start it over."""
    entry = await setup_balancer()
    for watts in range(1400, 1430):
        hass.states.async_set(UPSTAIRS_POWER, str(watts), {"unit_of_measurement": "W"})
        await hass.async_block_till_done()
    learned, _ = balancer_of(hass, entry).learned_draw(UPSTAIRS)
    assert learned > 0
    sensor = _entity_of(hass, entry, UPSTAIRS, "sensor", "learned_draw")
    assert float(hass.states.get(sensor).state) == learned

    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    await _set_balancing(hass, switch, on=False)

    assert balancer_of(hass, entry).learned_draw(UPSTAIRS)[0] == learned


async def test_an_appliance_cannot_be_added_twice(hass, world, setup_balancer):
    """Two rows for one heater would shed and restore it against itself."""
    entry = await setup_balancer()
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "appliance"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "entity_id": UPSTAIRS_POWER,
            "appliance": UPSTAIRS,
            "importance": 4,
            "last_resort": False,
        },
    )
    assert result["errors"] == {"appliance": "appliance_already_configured"}


async def test_editing_keeps_the_balancing_switch_where_it_was(
    hass, world, setup_balancer
):
    """The form does not own that choice; saving it must not flip it back."""
    entry = await setup_balancer()
    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    await _set_balancing(hass, switch, on=False)

    subentry = _subentry(entry, UPSTAIRS)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "appliance"),
        context={"source": "reconfigure", "subentry_id": subentry.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "entity_id": UPSTAIRS_POWER,
            "appliance": UPSTAIRS,
            "importance": 5,
            "last_resort": False,
        },
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert _subentry(entry, UPSTAIRS).data["importance"] == 5
    assert _subentry(entry, UPSTAIRS).data["balancing_enabled"] is False


async def test_a_repair_left_from_before_is_cleared_at_startup(
    hass, world, setup_balancer
):
    """An appliance put away is never judged again, so nothing else clears it."""
    entry = await setup_balancer()
    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    await _set_balancing(hass, switch, on=False)
    issue_id = f"balancing_entity_not_registered_{entry.entry_id}_" + (
        UPSTAIRS_POWER.replace(".", "_")
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="entity_not_registered",
    )

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_an_existing_sensor_turns_diagnostic_on_upgrade(
    hass, world, setup_balancer
):
    """Entities registered by v2.0.0 had no category; the next load sets it."""
    entry = await setup_balancer()
    registry = er.async_get(hass)
    entity_id = _entity_of(hass, entry, UPSTAIRS, "sensor", "learned_draw")
    registry.async_update_entity(entity_id, entity_category=None)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    found = registry.async_get(entity_id).entity_category
    assert found is not None
    assert found.value == "diagnostic"


async def test_a_device_rebuilt_at_startup_gets_its_entities_back(
    hass, world, setup_balancer
):
    """
    The water heater's device is removed and added back while it starts.

    Removal drops every other integration's link to it; the device comes back
    under its old id, and so must the balancer's entities.
    """
    device = _register_on_device(hass, UPSTAIRS, "generic_water_heater", "Upstairs")
    entry = await setup_balancer()
    devices, registry = dr.async_get(hass), er.async_get(hass)
    switch = _entity_of(hass, entry, UPSTAIRS, "switch", "appliance_balancing")
    assert registry.async_get(switch).device_id == device.id

    devices.async_remove_device(device.id)
    await hass.async_block_till_done()
    assert registry.async_get(switch).device_id is None
    assert registry.async_get(UPSTAIRS) is None, "the heater went with its device"

    rebuilt = _register_on_device(hass, UPSTAIRS, "generic_water_heater", "Upstairs")
    await hass.async_block_till_done()

    for platform, key in (
        ("switch", "appliance_balancing"),
        ("sensor", "learned_draw"),
        ("binary_sensor", "shed_by_balancer"),
    ):
        entity_id = _entity_of(hass, entry, UPSTAIRS, platform, key)
        assert registry.async_get(entity_id).device_id == rebuilt.id, key
    assert hass.states.get(switch).name == "Upstairs PLB enable"
