"""
Which missing entities are worth telling somebody about.

Five of the six repairs this integration was raising on the owner's system
were his seasonal heaters, switched off at the config entry for the summer.
A sixth named a sensor that has never existed here. None of them was a fault,
none of them would ever clear, and they came back every restart -- which is
how a Repairs list stops being read at all.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_load_balancer.const import (
    DOMAIN,
    UNAVAILABLE_DEBOUNCE_SECONDS,
)
from tests.conftest import (
    DOWNSTAIRS_POWER,
    UPSTAIRS_POWER,
    feed,
    ladder,
)

GHOST = "sensor.never_existed_here"


def _issues(hass) -> set[str]:
    """Return the issue ids this integration currently has raised."""
    return {
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    }


def _unavailable_issues(hass) -> set[str]:
    return {i for i in _issues(hass) if i.startswith("balancing_entity_unavailable")}


def _not_registered_issues(hass) -> set[str]:
    return {i for i in _issues(hass) if i.startswith("balancing_entity_not_registered")}


def _register(hass, entity_id, *, disabled_by=None):
    """Put an entity in the registry, optionally switched off."""
    domain, object_id = entity_id.split(".", 1)
    existing = hass.states.get(entity_id)
    if existing is not None:
        hass.states.async_remove(entity_id)
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        domain, "demo", f"health_{object_id}", suggested_object_id=object_id
    )
    if disabled_by is not None:
        registry.async_update_entity(entry.entity_id, disabled_by=disabled_by)
    assert registry.async_get(entity_id) is not None
    return entry


async def _settle(hass, clock=None, seconds: float = 0):
    """
    Let the balancer see the present state, then let time pass and look again.

    The first reading has to land before the clock moves: an outage is timed
    from when it was first noticed, so advancing first would restart the
    count on every check and nothing would ever pass the debounce.
    """
    await feed(hass, 3000)
    await hass.async_block_till_done()
    if clock is not None and seconds:
        await clock.advance(seconds)
        await feed(hass, 3001)
        await hass.async_block_till_done()


# --- a deliberate choice is not a fault -------------------------------------


@pytest.mark.parametrize(
    "disabled_by",
    [
        er.RegistryEntryDisabler.USER,
        er.RegistryEntryDisabler.CONFIG_ENTRY,
        er.RegistryEntryDisabler.DEVICE,
    ],
)
async def test_a_disabled_entity_never_raises_a_repair(
    hass, world, clock, setup_balancer, disabled_by
):
    """
    Switching a heater off for the summer is a decision, not a breakage.

    The state machine answers nothing for a disabled entity and nothing for a
    faulted one, so judging by state alone reports the owner's own choice back
    to him as a problem -- every restart, forever.
    """
    _register(hass, UPSTAIRS_POWER, disabled_by=disabled_by)
    await setup_balancer()

    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    assert not _unavailable_issues(hass), (
        f"raised a fault for an entity disabled by {disabled_by}"
    )
    assert not _not_registered_issues(hass)


async def test_a_disabled_entity_is_still_visible_in_diagnostics(
    hass, world, clock, setup_balancer
):
    """Not raising a repair must not mean hiding it from someone debugging."""
    _register(hass, UPSTAIRS_POWER, disabled_by=er.RegistryEntryDisabler.USER)
    entry = await setup_balancer()
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    balancer = hass.data[DOMAIN][entry.entry_id]
    health = dict(balancer._tracked_entities())
    assert health[UPSTAIRS_POWER] == "power_sensor"


# --- configured but non-existent is a different problem ---------------------


async def test_an_entity_that_never_existed_is_not_called_unavailable(
    hass, world, clock, setup_balancer
):
    """
    "Unavailable" says it might come back. This one cannot.

    Only editing the options fixes a row naming an entity that is not in this
    Home Assistant, so filing it as an outage sends the owner looking at a
    device instead of at his configuration.
    """
    rungs = ladder()
    rungs[0]["entity_id"] = GHOST
    await setup_balancer(power_sensors=rungs)

    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    assert _not_registered_issues(hass), "a non-existent entity went unreported"
    assert not _unavailable_issues(hass), (
        "a non-existent entity was reported as a transient outage"
    )


async def test_the_not_registered_notice_names_the_entity(
    hass, world, clock, setup_balancer
):
    """The owner has six rows; the notice has to say which one."""
    rungs = ladder()
    rungs[0]["entity_id"] = GHOST
    await setup_balancer(power_sensors=rungs)
    await _settle(hass, clock)

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, next(iter(_not_registered_issues(hass)))
    )
    assert issue.translation_placeholders["entity_id"] == GHOST


async def test_it_clears_once_the_entity_exists(hass, world, clock, setup_balancer):
    """Fixing the configuration, or the entity appearing, must end the notice."""
    rungs = ladder()
    rungs[0]["entity_id"] = GHOST
    await setup_balancer(power_sensors=rungs)
    await _settle(hass, clock)
    assert _not_registered_issues(hass)

    hass.states.async_set(GHOST, "0", {"unit_of_measurement": "W"})
    await _settle(hass, clock)

    assert not _not_registered_issues(hass)


# --- a restart is not an outage ---------------------------------------------


async def test_a_brief_outage_raises_nothing(hass, world, clock, setup_balancer):
    """
    Every entity is unavailable for a moment during a restart.

    Raising on the first miss turned an ordinary restart into a repair per
    balanced entity -- which is exactly what happened on the owner's system
    at 02:03 one morning, for a sensor that was healthy again by 02:04.
    """
    await setup_balancer()

    hass.states.async_set(UPSTAIRS_POWER, "unavailable")
    await _settle(hass, clock, 60)
    assert not _unavailable_issues(hass), "a one-minute gap was called a fault"

    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await _settle(hass, clock, 60)
    assert not _unavailable_issues(hass)


async def test_a_real_outage_is_still_reported(hass, world, clock, setup_balancer):
    """Debouncing must not mean never reporting."""
    await setup_balancer()

    hass.states.async_set(UPSTAIRS_POWER, "unavailable")
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    assert _unavailable_issues(hass), "a sustained outage went unreported"


async def test_a_recovered_entity_clears_itself(hass, world, clock, setup_balancer):
    """
    The stuck-repair bug: a notice outliving the condition that raised it.

    Clearing used to depend on catching the entity's own recovery event, so a
    recovery that happened while listeners were still being registered was
    never noticed and the notice stayed open indefinitely.
    """
    await setup_balancer()
    hass.states.async_set(UPSTAIRS_POWER, "unavailable")
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)
    assert _unavailable_issues(hass)

    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await _settle(hass, clock, 30)

    assert not _unavailable_issues(hass), "the notice outlived the outage"


async def test_recovery_is_noticed_without_its_own_event(
    hass, world, clock, setup_balancer
):
    """
    Head-on test for the missed-event race.

    The entity comes back silently -- its own listener never sees a thing --
    and the sweep still has to notice, because judging the present state is
    not the same as waiting to be told about a change.
    """
    await setup_balancer()
    hass.states.async_set(UPSTAIRS_POWER, "unavailable")
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)
    assert _unavailable_issues(hass)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    balancer = hass.data[DOMAIN][entry.entry_id]
    # Restore the state with the power-sensor listener torn down, so nothing
    # tells the balancer about the recovery.
    if balancer._monitored_sensors_unsub is not None:
        balancer._monitored_sensors_unsub()
        balancer._monitored_sensors_unsub = None
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()

    balancer._evaluate_entity_health()  # the periodic sweep
    await hass.async_block_till_done()

    assert not _unavailable_issues(hass), (
        "only an entity's own event could clear it, so a silent recovery could not"
    )


async def test_an_outage_and_a_ghost_are_reported_separately(
    hass, world, clock, setup_balancer
):
    """Two different problems on one ladder must not be merged into one."""
    rungs = ladder()
    rungs[0]["entity_id"] = GHOST
    await setup_balancer(power_sensors=rungs)

    hass.states.async_set(DOWNSTAIRS_POWER, "unavailable")
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    assert len(_not_registered_issues(hass)) == 1
    assert len(_unavailable_issues(hass)) == 1


async def test_nothing_is_judged_before_home_assistant_has_started(
    hass, world, setup_balancer
):
    """Setup itself must not raise; the first judgement waits for startup."""
    rungs = ladder()
    rungs[0]["entity_id"] = GHOST
    entry = await setup_balancer(power_sensors=rungs)
    balancer = hass.data[DOMAIN][entry.entry_id]

    assert balancer._health_started_unsub is not None
    assert entry.entry_id


async def test_unloading_withdraws_everything_it_raised(
    hass, world, clock, setup_balancer
):
    """Removing the integration must not leave its notices behind."""
    rungs = ladder()
    rungs[0]["entity_id"] = GHOST
    entry = await setup_balancer(power_sensors=rungs)
    hass.states.async_set(DOWNSTAIRS_POWER, "unavailable")
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)
    assert _issues(hass)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not _issues(hass), "notices survived the integration being unloaded"


async def test_the_main_sensor_is_judged_the_same_way(
    hass, world, clock, setup_balancer
):
    """The meter is the one entity balancing cannot do without."""
    await setup_balancer(main_power_sensor=GHOST)
    await _settle(hass, clock)

    assert _not_registered_issues(hass)
    assert not _unavailable_issues(hass)


async def test_the_owners_actual_ladder_reports_only_what_he_can_act_on(
    hass, world, clock, setup_balancer
):
    """
    The shape that prompted all this: six rungs, five bogus repairs.

    Two rungs name power sensors that are not in this Home Assistant, one rung
    is a heater switched off at its config entry for the summer, and the rest
    are healthy but idle. Only the two misconfigured rows are anybody's to
    fix, so only those two should say anything.
    """
    disabled_sensor = "sensor.seasonal_heater_power"
    disabled_appliance = "climate.seasonal_heater"
    _register(hass, disabled_sensor, disabled_by=er.RegistryEntryDisabler.CONFIG_ENTRY)
    _register(hass, disabled_appliance, disabled_by=er.RegistryEntryDisabler.DEVICE)

    rungs = ladder()
    rungs.extend(
        [
            {
                "appliance": disabled_appliance,
                "entity_id": disabled_sensor,
                "name": "Seasonal Heater",
                "importance": 3,
            },
            {
                "appliance": "climate.bedroom_heater",
                "entity_id": "sensor.bedroom_heater_power",
                "name": "Bedroom Heater",
                "importance": 4,
            },
            {
                "appliance": "climate.lounge_heater",
                "entity_id": "sensor.lounge_heater_power",
                "name": "Lounge Heater",
                "importance": 5,
            },
        ]
    )
    # The two appliances are live and fine; only their power sensors are ghosts.
    for appliance in ("climate.bedroom_heater", "climate.lounge_heater"):
        hass.states.async_set(appliance, "off", {"operation_list": ["heat", "off"]})

    await setup_balancer(power_sensors=rungs)
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    assert _not_registered_issues(hass) == {
        f"balancing_entity_not_registered_{hass.config_entries.async_entries(DOMAIN)[0].entry_id}_sensor_bedroom_heater_power",
        f"balancing_entity_not_registered_{hass.config_entries.async_entries(DOMAIN)[0].entry_id}_sensor_lounge_heater_power",
    }, _not_registered_issues(hass)
    assert not _unavailable_issues(hass), (
        "the seasonal heater was reported as a fault again"
    )


# --- deleted, but its integration is only switched off ----------------------


def _delete_entity_leaving_a_record(hass, entity_id, config_entry):
    """Register an entity against an entry, then remove it as HA would."""
    domain, object_id = entity_id.split(".", 1)
    registry = er.async_get(hass)
    existing = hass.states.get(entity_id)
    if existing is not None:
        hass.states.async_remove(entity_id)
    registry.async_get_or_create(
        domain,
        "tplink",
        f"unique_{object_id}",
        suggested_object_id=object_id,
        config_entry=config_entry,
    )
    registry.async_remove(entity_id)
    assert registry.async_get(entity_id) is None
    assert any(
        record.entity_id == entity_id for record in registry.deleted_entities.values()
    ), "fixture left no deletion record"


async def test_an_entity_a_disabled_integration_will_recreate_is_not_a_fault(
    hass, world, clock, setup_balancer
):
    """
    Switching an integration off deletes some of its entities outright.

    Others it keeps and marks disabled -- the same shutdown, two different
    registry outcomes. Both come back when the entry is re-enabled, so
    calling the deleted one a misconfiguration would tell somebody their
    seasonal shutdown is a permanent mistake, every winter.
    """
    owner = MockConfigEntry(domain="fake_plug", title="Heater Plug")
    owner.add_to_hass(hass)
    await hass.config_entries.async_set_disabled_by(
        owner.entry_id, ConfigEntryDisabler.USER
    )
    _delete_entity_leaving_a_record(hass, UPSTAIRS_POWER, owner)

    await setup_balancer()
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    assert not _not_registered_issues(hass), (
        "a seasonal shutdown was reported as a permanent misconfiguration"
    )
    assert not _unavailable_issues(hass)


async def test_a_deletion_whose_integration_is_gone_is_still_a_misconfiguration(
    hass, world, clock, setup_balancer
):
    """No entry left to recreate it means nothing will bring it back."""
    owner = MockConfigEntry(domain="fake_plug", title="Removed Plug")
    owner.add_to_hass(hass)
    _delete_entity_leaving_a_record(hass, UPSTAIRS_POWER, owner)
    # The entry itself is then removed, leaving the deletion record orphaned.
    await hass.config_entries.async_remove(owner.entry_id)
    await hass.async_block_till_done()

    await setup_balancer()
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    assert _not_registered_issues(hass), (
        "an entity nothing can recreate went unreported"
    )


async def test_a_deletion_under_a_live_integration_is_a_misconfiguration(
    hass, world, clock, setup_balancer
):
    """An enabled entry has had its chance to create it and has not."""
    owner = MockConfigEntry(domain="fake_plug", title="Live Plug")
    owner.add_to_hass(hass)
    _delete_entity_leaving_a_record(hass, UPSTAIRS_POWER, owner)

    await setup_balancer()
    await _settle(hass, clock, UNAVAILABLE_DEBOUNCE_SECONDS + 60)

    assert _not_registered_issues(hass)
