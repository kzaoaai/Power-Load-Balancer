"""
Where each kind of balancer event gets delivered.

Delivery is a per-category choice -- nothing, a card in Home Assistant, a
push to a phone, or both -- because a shed and a budget change do not deserve
the same treatment. Whatever is chosen, filtering has to stop at the
notification channels: the event log and the bus event are the record of what
the balancer did, and a record with holes in it is worse than none at all
when working out afterwards why a tank went cold.
"""

from __future__ import annotations

from homeassistant.data_entry_flow import FlowResultType

from custom_components.power_load_balancer.const import DOMAIN
from tests.conftest import (
    ARM_LEVEL,
    DOWNSTAIRS,
    TANK_VA,
    UPSTAIRS,
    UPSTAIRS_POWER,
    hold,
)

NOTIFY_SERVICE = "mobile_app_test"

ALL_NONE = {
    "shed": "none",
    "restore": "none",
    "budget": "none",
    "problem": "none",
}


def _pushed(recorder) -> list[str]:
    """Return the messages pushed through the notify service."""
    return [message for domain, _, message in recorder.calls if domain == "notify"]


def _cards(hass) -> list[str]:
    """Return the ids of the persistent notifications now showing."""
    return list(hass.data.get("persistent_notification", {}))


async def _with_channels(hass, setup_balancer, recorder, channels, **overrides: object):
    """Set the balancer up with a given per-category delivery mapping."""

    async def _handle(call):
        recorder.calls.append(("notify", NOTIFY_SERVICE, call.data.get("message")))

    hass.services.async_register("notify", NOTIFY_SERVICE, _handle)
    options = {"notify_service": NOTIFY_SERVICE, "notify_channels": channels}
    options.update(overrides)
    return await setup_balancer(**options)


async def _shed_a_tank(hass, clock) -> None:
    hass.states.async_set(UPSTAIRS, "electric", {"operation_list": ["electric", "off"]})
    hass.states.async_set(UPSTAIRS_POWER, str(TANK_VA), {"unit_of_measurement": "W"})
    await hold(hass, ARM_LEVEL + 60, 75, clock)


async def test_push_only_goes_to_the_device(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """A push goes to the phone, and not to a card as well."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_channels(hass, setup_balancer, world, {**ALL_NONE, "shed": "service"})
    await _shed_a_tank(hass, clock)

    assert _pushed(world), "asked for a push and got none"
    assert not _cards(hass), "raised a Home Assistant card that was not asked for"


async def test_a_card_only_goes_to_home_assistant(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """And the reverse: a card must not also ring the phone."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_channels(
        hass, setup_balancer, world, {**ALL_NONE, "shed": "persistent"}
    )
    await _shed_a_tank(hass, clock)

    assert _cards(hass), "asked for a Home Assistant card and got none"
    assert not _pushed(world), "pushed to the device without being asked"


async def test_both_means_both(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """The whole point of the picker is that both is a distinct answer."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_channels(hass, setup_balancer, world, {**ALL_NONE, "shed": "both"})
    await _shed_a_tank(hass, clock)

    assert _pushed(world)
    assert _cards(hass)


async def test_none_is_silent_on_every_channel(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Nothing selected must mean nothing sent, by either route."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_channels(hass, setup_balancer, world, dict(ALL_NONE))
    await _shed_a_tank(hass, clock)

    assert not _pushed(world)
    assert not _cards(hass)


async def test_categories_are_routed_independently(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A shed can page the phone while a budget change stays quiet.

    This is the request: one line per kind of event, each with its own
    answer. A single global switch cannot express it.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_channels(
        hass,
        setup_balancer,
        world,
        {**ALL_NONE, "shed": "service", "budget": "persistent"},
    )

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.power_load_balancer_power_budget", "value": 6500},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert _cards(hass), "budget change was set to a card and did not raise one"
    assert not _pushed(world), "budget change pushed to the device anyway"

    await _shed_a_tank(hass, clock)

    assert _pushed(world), "the shed was set to push and did not"


async def test_the_record_keeps_everything_regardless(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    The regression that matters: silencing must not eat the record.

    Choosing not to be told about sheds means "do not interrupt me", not "do
    not write it down". The log is what a person reads afterwards to work out
    what happened.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_channels(hass, setup_balancer, world, dict(ALL_NONE))

    events: list[tuple[str, str]] = []
    hass.bus.async_listen(
        "power_load_balancer_event",
        lambda e: events.append((e.data["category"], e.data["message"])),
    )

    await _shed_a_tank(hass, clock)

    log = hass.states.get("sensor.power_load_balancer_log")
    assert any("Turned off" in entry for entry in log.attributes["events"]), (
        "the shed was filtered out of the event log, not just the notification"
    )
    assert any(category == "shed" for category, _ in events), (
        "the shed never reached the event bus"
    )
    assert not _pushed(world)
    assert not _cards(hass)


async def test_every_event_carries_a_category(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """An untagged event would be unroutable and land in the wrong bucket."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_channels(hass, setup_balancer, world, dict(ALL_NONE))

    seen: list[str] = []
    hass.bus.async_listen(
        "power_load_balancer_event", lambda e: seen.append(e.data["category"])
    )

    await _shed_a_tank(hass, clock)
    hass.states.async_set(UPSTAIRS_POWER, "0", {"unit_of_measurement": "W"})
    await hold(hass, 2000, 40, clock)

    assert seen, "no events at all"
    assert all(
        category in {"shed", "restore", "budget", "problem"} for category in seen
    ), seen


async def test_a_push_with_no_service_configured_is_not_attempted(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Belt and braces for the config-flow guard: never call notify with no name."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await _with_channels(
        hass,
        setup_balancer,
        world,
        {**ALL_NONE, "shed": "both"},
        notify_service="",
    )
    await _shed_a_tank(hass, clock)

    assert _cards(hass), "the half that was deliverable should still arrive"
    assert not _pushed(world)


# --- settings saved before delivery became a per-category choice -------------


async def test_an_upgrade_never_narrows_what_was_being_delivered(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    An entry with no category list was written before categories existed.

    That code notified about everything, so translating it into a narrower
    default would take two kinds of notification away from someone who never
    chose to lose them -- silently, which is the exact failure this feature
    exists to prevent.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)

    async def _handle(call):
        world.calls.append(("notify", NOTIFY_SERVICE, call.data.get("message")))

    hass.services.async_register("notify", NOTIFY_SERVICE, _handle)
    await setup_balancer(notify_persistent=True, notify_service=NOTIFY_SERVICE)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.power_load_balancer_power_budget", "value": 6500},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert _pushed(world), (
        "a budget change used to be pushed and stopped being pushed after the "
        "upgrade, without anyone choosing that"
    )
    assert _cards(hass)


async def test_an_empty_category_list_is_still_honoured(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """Absent means "never chose"; empty means "chose nothing". Not the same."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)

    async def _handle(call):
        world.calls.append(("notify", NOTIFY_SERVICE, call.data.get("message")))

    hass.services.async_register("notify", NOTIFY_SERVICE, _handle)
    await setup_balancer(
        notify_persistent=True,
        notify_service=NOTIFY_SERVICE,
        notify_categories=[],
    )

    await _shed_a_tank(hass, clock)

    assert not _pushed(world)
    assert not _cards(hass)


async def test_older_settings_keep_working(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    An entry saved before this change must not go quiet.

    It carries one persistent-notification switch and a list of selected
    categories; those have to map onto the new shape without anyone opening
    the settings page, or an alert silently stops arriving.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)

    async def _handle(call):
        world.calls.append(("notify", NOTIFY_SERVICE, call.data.get("message")))

    hass.services.async_register("notify", NOTIFY_SERVICE, _handle)
    await setup_balancer(
        notify_persistent=True,
        notify_service=NOTIFY_SERVICE,
        notify_categories=["shed"],
    )

    await _shed_a_tank(hass, clock)

    assert _pushed(world), "an upgrade silenced a push that used to arrive"
    assert _cards(hass), "an upgrade silenced a card that used to appear"


async def test_older_settings_keep_their_exclusions(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """
    A category that was deselected must stay deselected across the upgrade.

    Asserting only the silence would pass just as well against a migration
    that dropped everything, so this also proves the selected category still
    arrives.
    """
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)

    async def _handle(call):
        world.calls.append(("notify", NOTIFY_SERVICE, call.data.get("message")))

    hass.services.async_register("notify", NOTIFY_SERVICE, _handle)
    await setup_balancer(
        notify_persistent=True,
        notify_service=NOTIFY_SERVICE,
        notify_categories=["budget"],
    )

    await _shed_a_tank(hass, clock)
    assert not _pushed(world), "notified about a category that was deselected"
    assert not _cards(hass)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.power_load_balancer_power_budget", "value": 6500},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert _pushed(world), "the selected category stopped arriving after the upgrade"
    assert _cards(hass)


async def test_a_brand_new_entry_is_quiet_until_asked(
    hass, world, clock, setup_balancer, register_shed_platform
):
    """No notification settings at all means no notifications, as before."""
    register_shed_platform(UPSTAIRS, DOWNSTAIRS)
    await setup_balancer()
    await _shed_a_tank(hass, clock)

    assert not _cards(hass)


# --- the settings page ------------------------------------------------------


async def test_one_line_per_category_with_its_own_picker(hass, world, setup_balancer):
    """The form AA asked for: a row per event kind, each with a choice."""
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    offered = {str(key) for key in result["data_schema"].schema}

    assert offered == {
        "notify_service",
        "notify_channel_shed",
        "notify_channel_restore",
        "notify_channel_budget",
        "notify_channel_problem",
    }, offered
    assert "notify_persistent" not in offered, "the global switch should be gone"


async def test_settings_have_their_own_page(hass, world, setup_balancer):
    """They say nothing about the meter, so they do not belong in its form."""
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    menu = next(key for key in result["data_schema"].schema if str(key) == "action")
    assert "edit_notifications" in result["data_schema"].schema[menu].container

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_main_sensor"}
    )
    offered = {str(key) for key in result["data_schema"].schema}
    assert not any(field.startswith("notify") for field in offered), (
        "notification settings are still buried in the main sensor form"
    )


async def test_the_picker_round_trips(hass, world, setup_balancer):
    """Whatever is chosen must still be chosen next time."""
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "edit_notifications"

    chosen = {
        "shed": "both",
        "restore": "persistent",
        "budget": "none",
        "problem": "service",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_service": NOTIFY_SERVICE,
            **{f"notify_channel_{k}": v for k, v in chosen.items()},
        },
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "finish"}
    )
    await hass.async_block_till_done()

    assert entry.options["notify_channels"] == chosen
    assert entry.options["notify_service"] == NOTIFY_SERVICE

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    defaults = {
        str(key).removeprefix("notify_channel_"): key.default()
        for key in result["data_schema"].schema
        if str(key).startswith("notify_channel_")
    }
    assert defaults == chosen, defaults


async def test_asking_for_a_push_with_no_device_is_refused(hass, world, setup_balancer):
    """
    A push with nowhere to go looks configured and delivers nothing.

    That is precisely how an alert goes unnoticed for weeks, so the form
    blocks it rather than saving something inert.
    """
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_service": "none",
            "notify_channel_shed": "service",
            "notify_channel_restore": "none",
            "notify_channel_budget": "none",
            "notify_channel_problem": "persistent",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "notify_service_required"}


async def test_a_refused_form_keeps_what_was_typed(hass, world, setup_balancer):
    """Re-showing an empty form would make the user redo every row."""
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_service": "none",
            "notify_channel_shed": "service",
            "notify_channel_restore": "none",
            "notify_channel_budget": "none",
            "notify_channel_problem": "persistent",
        },
    )

    defaults = {
        str(key): key.default()
        for key in result["data_schema"].schema
        if str(key).startswith("notify_channel_")
    }
    assert defaults["notify_channel_shed"] == "service"
    assert defaults["notify_channel_problem"] == "persistent"


async def test_a_typed_service_name_is_cleaned_before_it_is_trusted(
    hass, world, setup_balancer
):
    """
    The service picker takes a typed value, and the guard trusts its output.

    "notify. mobile_app_x" left a leading space on the stored name: non-empty,
    so the guard passed it, and every send then failed with ServiceNotFound --
    logged once and suppressed. Configured-looking and undeliverable, which is
    precisely what the guard exists to stop.
    """
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_service": "notify.  mobile_app_x  ",
            "notify_channel_shed": "service",
            "notify_channel_restore": "none",
            "notify_channel_budget": "none",
            "notify_channel_problem": "none",
        },
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "finish"}
    )
    await hass.async_block_till_done()

    assert entry.options["notify_service"] == "mobile_app_x"


async def test_saving_records_the_new_shape(hass, world, setup_balancer):
    """The stored value is the mapping, not a switch plus a list."""
    entry = await setup_balancer()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_notifications"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_service": "none",
            "notify_channel_shed": "persistent",
            "notify_channel_restore": "none",
            "notify_channel_budget": "none",
            "notify_channel_problem": "persistent",
        },
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "finish"}
    )
    await hass.async_block_till_done()

    assert entry.options["notify_channels"]["shed"] == "persistent"
    assert entry.options["notify_service"] == ""
    assert DOMAIN
