"""
Choosing which events are worth interrupting someone for.

Filtering has to stop at the notification channels. The event log and the bus
event are the record of what the balancer did, and a record with holes in it
is worse than none at all when working out afterwards why a tank went cold.
"""

from __future__ import annotations

from homeassistant.data_entry_flow import FlowResultType

from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    hold,
)

NOTIFY_SERVICE = "mobile_app_test"


def _notified(recorder) -> list[str]:
    return [service for domain, service, _ in recorder.calls if domain == "notify"]


async def _with_categories(hass, setup_balancer, recorder, categories):
    """Set the balancer up notifying only about the given categories."""

    async def _handle(call):
        recorder.calls.append(("notify", NOTIFY_SERVICE, call.data.get("message")))

    hass.services.async_register("notify", NOTIFY_SERVICE, _handle)
    return await setup_balancer(
        notify_service=NOTIFY_SERVICE,
        notify_categories=categories,
    )


async def _shed_a_tank(hass, clock, world) -> None:
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)


async def test_a_shed_notifies_when_sheds_are_selected(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """The obvious case: ask for shed notifications, get one."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_categories(hass, setup_balancer, world, ["shed"])
    await _shed_a_tank(hass, clock, world)

    assert _notified(world), "asked to be told about sheds and was not"


async def test_a_shed_is_silent_when_sheds_are_not_selected(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Deselecting a category must actually stop the notification."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_categories(hass, setup_balancer, world, ["budget"])
    await _shed_a_tank(hass, clock, world)

    assert not _notified(world), "notified about a category that was not selected"


async def test_the_event_log_keeps_everything_regardless(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    The regression that matters: filtering must not eat the record.

    Turning off shed notifications means "do not interrupt me", not "do not
    write it down". The log is what a person reads afterwards to work out
    what happened.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_categories(hass, setup_balancer, world, ["budget"])

    events: list[tuple[str, str]] = []
    hass.bus.async_listen(
        "power_load_balancer_event",
        lambda e: events.append((e.data["category"], e.data["message"])),
    )

    await _shed_a_tank(hass, clock, world)

    log = hass.states.get("sensor.power_load_balancer_log")
    assert any("Turned off" in entry for entry in log.attributes["events"]), (
        "the shed was filtered out of the event log, not just the notification"
    )
    assert any(category == "shed" for category, _ in events), (
        "the shed never reached the event bus"
    )
    assert not _notified(world)


async def test_every_event_carries_a_category(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An untagged event would be unfilterable and land in the wrong bucket."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_categories(hass, setup_balancer, world, ["shed", "problem"])

    seen: list[str] = []
    hass.bus.async_listen(
        "power_load_balancer_event", lambda e: seen.append(e.data["category"])
    )

    await _shed_a_tank(hass, clock, world)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hold(hass, 2000, 40, clock)

    assert seen, "no events at all"
    assert all(
        category in {"shed", "restore", "budget", "problem"} for category in seen
    ), seen


async def test_a_budget_change_is_its_own_category(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Budget moves are frequent and dull; they must be separable."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    entry = await _with_categories(hass, setup_balancer, world, ["shed"])
    world.clear()

    await hass.services.async_call(
        "number",
        "set_value",
        {
            "entity_id": "number.power_load_balancer_power_budget",
            "value": 6500,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert not _notified(world), "budget change notified despite not being selected"
    assert entry.entry_id


async def test_selecting_nothing_silences_notifications_only(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An empty selection is a valid choice, not a fall-back to everything."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_categories(hass, setup_balancer, world, [])

    events: list[str] = []
    hass.bus.async_listen(
        "power_load_balancer_event", lambda e: events.append(e.data["message"])
    )
    await _shed_a_tank(hass, clock, world)

    assert not _notified(world)
    assert events, "silencing notifications also silenced the record"


async def test_notification_settings_have_their_own_page(hass, world, setup_balancer):
    """They say nothing about the meter, so they do not belong in its form."""
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    menu = next(key for key in result["data_schema"].schema if str(key) == "action")
    choices = result["data_schema"].schema[menu].container
    assert "edit_notifications" in choices, choices

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_main_sensor"}
    )
    offered = {str(key) for key in result["data_schema"].schema}
    assert "notify_service" not in offered, (
        "notification settings are still buried in the main sensor form"
    )
    assert "notify_categories" not in offered


async def test_category_selection_round_trips(hass, world, setup_balancer):
    """Whatever is ticked must still be ticked next time."""
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "edit_notifications"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_persistent": True,
            "notify_service": NOTIFY_SERVICE,
            "notify_categories": ["restore", "problem"],
        },
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "finish"}
    )
    await hass.async_block_till_done()

    assert entry.options["notify_categories"] == ["restore", "problem"]
    assert entry.options["notify_persistent"] is True
    assert entry.options["notify_service"] == NOTIFY_SERVICE

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    default = next(
        key.default()
        for key in result["data_schema"].schema
        if str(key) == "notify_categories"
    )
    assert default == ["restore", "problem"], default
