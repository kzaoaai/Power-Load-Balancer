"""
Config flow for Power Load Balancer integration.

This module handles the configuration UI for setting up the Power Load Balancer,
including the main power sensor configuration and monitored appliance setup.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_ENTITY_ID,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .const import (
    BUDGET_MODE_FIXED,
    BUDGET_MODE_SOURCE,
    BUDGET_MODES,
    BUDGET_SOURCE_HISTORY_DAYS,
    BUDGET_SOURCE_MAX_STATES,
    CHANNEL_BOTH,
    CHANNEL_NONE,
    CHANNEL_SERVICE,
    CONF_APPLIANCE,
    CONF_BALANCING_ENABLED,
    CONF_BUDGET_MODE,
    CONF_BUDGET_RAISE_DELAY_SECONDS,
    CONF_BUDGET_SOURCE_ENTITY,
    CONF_BUDGET_SOURCE_MAP,
    CONF_COOLDOWN_SECONDS,
    CONF_DEVICE_COOLDOWN,
    CONF_IMPORTANCE,
    CONF_LAST_RESORT,
    CONF_MAIN_POWER_SENSOR,
    CONF_NOMINAL_POWER_WATT,
    CONF_NOTIFY_CHANNELS,
    CONF_NOTIFY_SERVICES,
    CONF_POWER_BUDGET_WATT,
    CONF_POWER_DRAW_MODE,
    CONF_SUSTAINED_DURATION_SECONDS,
    CONF_SUSTAINED_ENABLED,
    CONF_SUSTAINED_THRESHOLD_PERCENT,
    DEFAULT_BUDGET_RAISE_DELAY_SECONDS,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_NOMINAL_POWER_WATT,
    DEFAULT_POWER_DRAW_MODE,
    DEFAULT_SUSTAINED_DURATION_SECONDS,
    DEFAULT_SUSTAINED_ENABLED,
    DEFAULT_SUSTAINED_THRESHOLD_PERCENT,
    DOMAIN,
    NOTIFY_CATEGORIES,
    NOTIFY_CHANNELS,
    POWER_DRAW_FIXED,
    POWER_DRAW_MODES,
    SUBENTRY_APPLIANCE,
    SUPPORTED_APPLIANCE_DOMAINS,
)
from .draw_learning import resolve_power_draw_mode
from .notify_channels import (
    resolve_notify_channels,
    resolve_notify_services,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceRegistry
    from homeassistant.helpers.entity_registry import EntityRegistry

try:
    from homeassistant.helpers.device_registry import (
        async_get as async_get_device_registry,
    )
    from homeassistant.helpers.entity_registry import (
        async_get as async_get_entity_registry,
    )
except ImportError:
    async_get_device_registry = None
    async_get_entity_registry = None

_LOGGER = logging.getLogger(__name__)

DEFAULT_IMPORTANCE = 5


def _get_power_sensor_selector() -> EntitySelector:
    """Return the entity selector for power sensors."""
    return EntitySelector(
        EntitySelectorConfig(domain="sensor", device_class=["power", "apparent_power"])
    )


def _sustained_schema_fields(config_data: dict[str, Any]) -> dict[Any, Any]:
    """Return the schema fields for the sustained-load shedding options."""
    return {
        vol.Required(
            CONF_SUSTAINED_ENABLED,
            default=bool(
                config_data.get(CONF_SUSTAINED_ENABLED, DEFAULT_SUSTAINED_ENABLED)
            ),
        ): bool,
        vol.Required(
            CONF_SUSTAINED_THRESHOLD_PERCENT,
            default=config_data.get(
                CONF_SUSTAINED_THRESHOLD_PERCENT,
                DEFAULT_SUSTAINED_THRESHOLD_PERCENT,
            ),
        ): NumberSelector(
            NumberSelectorConfig(
                min=30,
                max=100,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="%",
            )
        ),
        vol.Required(
            CONF_SUSTAINED_DURATION_SECONDS,
            default=config_data.get(
                CONF_SUSTAINED_DURATION_SECONDS,
                DEFAULT_SUSTAINED_DURATION_SECONDS,
            ),
        ): NumberSelector(
            NumberSelectorConfig(
                min=10,
                max=3600,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
    }


def _channel_field(category: str) -> str:
    """Return the form field name carrying one category's delivery choice."""
    return f"notify_channel_{category}"


def _notify_schema_fields(
    hass: HomeAssistant, config_data: dict[str, Any]
) -> dict[Any, Any]:
    """Return the schema fields for the balancer-event notification options."""
    available = sorted(hass.services.async_services().get("notify", {}))
    current = resolve_notify_services(config_data)
    options = [
        SelectOptionDict(value=service, label=f"notify.{service}")
        for service in available
    ]
    # A device that is offline right now still has to stay selectable, or
    # reopening this page would quietly drop it from the selection.
    options.extend(
        SelectOptionDict(
            value=service, label=f"notify.{service} (not currently loaded)"
        )
        for service in current
        if service not in available
    )
    channels = resolve_notify_channels(config_data)
    fields: dict[Any, Any] = {
        vol.Optional(CONF_NOTIFY_SERVICES, default=list(current)): SelectSelector(
            SelectSelectorConfig(
                options=options,
                mode=SelectSelectorMode.DROPDOWN,
                custom_value=True,
                multiple=True,
            )
        ),
    }
    # One line per kind of event, each with its own delivery choice: a shed
    # and a budget change rarely deserve the same treatment.
    for category in NOTIFY_CATEGORIES:
        fields[
            vol.Required(
                _channel_field(category),
                default=channels.get(category, CHANNEL_NONE),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=list(NOTIFY_CHANNELS),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="notify_channel",
            )
        )
    return fields


def _notify_services_from_input(user_input: dict[str, Any]) -> list[str]:
    """Return the notify service names a submitted form selected."""
    submitted = user_input.get(CONF_NOTIFY_SERVICES) or []
    if isinstance(submitted, str):
        submitted = [submitted]
    if not isinstance(submitted, list):
        return []

    services: list[str] = []
    for raw in submitted:
        if not isinstance(raw, str):
            continue
        # The picker allows typed values, so strip on both sides of the
        # prefix: "notify. mobile_app_x" would otherwise keep a leading
        # space, pass the guard below as a non-empty name, and fail every
        # send.
        service = raw.strip().removeprefix("notify.").strip()
        if service and service not in services:
            services.append(service)
    return services


def _validate_notify_options(user_input: dict[str, Any]) -> dict[str, str]:
    """
    Return form errors for an inconsistent notification setup.

    Asking for a push with no service behind it is the one mistake worth
    blocking: it looks configured and delivers nothing, which is exactly how
    an alert goes unnoticed for weeks.
    """
    if _notify_services_from_input(user_input):
        return {}
    wants_service = any(
        user_input.get(_channel_field(category)) in (CHANNEL_SERVICE, CHANNEL_BOTH)
        for category in NOTIFY_CATEGORIES
    )
    return {"base": "notify_service_required"} if wants_service else {}


def _store_notify_options(
    config_data: dict[str, Any], user_input: dict[str, Any]
) -> None:
    """Store validated notification options into config data."""
    config_data[CONF_NOTIFY_SERVICES] = _notify_services_from_input(user_input)

    channels: dict[str, str] = {}
    for category in NOTIFY_CATEGORIES:
        channel = user_input.get(_channel_field(category), CHANNEL_NONE)
        channels[category] = (
            str(channel) if channel in NOTIFY_CHANNELS else CHANNEL_NONE
        )
    config_data[CONF_NOTIFY_CHANNELS] = channels


def _store_sustained_options(
    config_data: dict[str, Any], user_input: dict[str, Any]
) -> None:
    """Store validated sustained-load shedding options into config data."""
    config_data[CONF_SUSTAINED_ENABLED] = bool(
        user_input.get(CONF_SUSTAINED_ENABLED, DEFAULT_SUSTAINED_ENABLED)
    )
    threshold = user_input.get(CONF_SUSTAINED_THRESHOLD_PERCENT)
    config_data[CONF_SUSTAINED_THRESHOLD_PERCENT] = (
        int(threshold)
        if isinstance(threshold, (int, float))
        else DEFAULT_SUSTAINED_THRESHOLD_PERCENT
    )
    duration = user_input.get(CONF_SUSTAINED_DURATION_SECONDS)
    config_data[CONF_SUSTAINED_DURATION_SECONDS] = (
        int(duration)
        if isinstance(duration, (int, float))
        else DEFAULT_SUSTAINED_DURATION_SECONDS
    )


def _get_appliance_selector() -> EntitySelector:
    """Return the entity selector for controllable appliances."""
    return EntitySelector(
        EntitySelectorConfig(domain=list(SUPPORTED_APPLIANCE_DOMAINS))
    )


def _power_draw_mode_selector() -> SelectSelector:
    """Return the learn-or-fixed choice for an appliance's draw."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(POWER_DRAW_MODES),
            mode=SelectSelectorMode.LIST,
            translation_key="power_draw_mode",
        )
    )


def _nominal_power_selector() -> NumberSelector:
    """Return the field for a fixed power draw, in watts."""
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=20000,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="W",
        )
    )


def _build_sensor_edit_schema(initial_data: dict[str, Any]) -> vol.Schema:
    """
    Build a schema for editing a sensor configuration.

    Args:
        initial_data: Current values to use as defaults.

    Returns:
        A voluptuous schema with the current values as defaults.

    """
    schema_dict: dict[Any, Any] = {}

    entity_id = initial_data.get(CONF_ENTITY_ID)
    if entity_id is not None:
        schema_dict[vol.Required(CONF_ENTITY_ID, default=entity_id)] = (
            _get_power_sensor_selector()
        )
    else:
        schema_dict[vol.Required(CONF_ENTITY_ID)] = _get_power_sensor_selector()

    name = initial_data.get(CONF_NAME)
    if name is not None:
        schema_dict[vol.Optional(CONF_NAME, default=name)] = TextSelector(
            TextSelectorConfig()
        )
    else:
        schema_dict[vol.Optional(CONF_NAME)] = TextSelector(TextSelectorConfig())

    importance = initial_data.get(CONF_IMPORTANCE, DEFAULT_IMPORTANCE)
    schema_dict[vol.Required(CONF_IMPORTANCE, default=importance)] = NumberSelector(
        NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
    )

    last_resort = initial_data.get(CONF_LAST_RESORT, False)
    schema_dict[vol.Required(CONF_LAST_RESORT, default=last_resort)] = bool

    appliance = initial_data.get(CONF_APPLIANCE)
    if appliance is not None:
        schema_dict[vol.Required(CONF_APPLIANCE, default=appliance)] = (
            _get_appliance_selector()
        )
    else:
        schema_dict[vol.Required(CONF_APPLIANCE)] = _get_appliance_selector()

    device_cooldown = initial_data.get(CONF_DEVICE_COOLDOWN)
    default_cooldown_value = device_cooldown if device_cooldown is not None else -1
    schema_dict[vol.Optional(CONF_DEVICE_COOLDOWN, default=default_cooldown_value)] = (
        NumberSelector(
            NumberSelectorConfig(
                min=-1,
                max=3600,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        )
    )

    schema_dict[
        vol.Required(
            CONF_POWER_DRAW_MODE, default=resolve_power_draw_mode(initial_data)
        )
    ] = _power_draw_mode_selector()
    nominal_power = initial_data.get(
        CONF_NOMINAL_POWER_WATT, DEFAULT_NOMINAL_POWER_WATT
    )
    schema_dict[vol.Optional(CONF_NOMINAL_POWER_WATT, default=nominal_power)] = (
        _nominal_power_selector()
    )

    return vol.Schema(schema_dict)


def _get_friendly_name_for_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """
    Return a friendly name for the device connected to the given sensor.

    Tries to get the device name from the device registry.
    Falls back to the entity's original_name or name.

    Args:
        hass: Home Assistant instance.
        entity_id: The entity ID to look up.

    Returns:
        A friendly name string, or None if unavailable.

    """
    if async_get_device_registry is None or async_get_entity_registry is None:
        return None

    entity_registry: EntityRegistry | None = async_get_entity_registry(hass)
    if entity_registry is None:
        return None

    entity = entity_registry.entities.get(str(entity_id))
    if entity is None:
        return None

    result: str | None = None

    if entity.device_id:
        device_registry: DeviceRegistry | None = async_get_device_registry(hass)
        if device_registry is not None:
            device = device_registry.devices.get(entity.device_id)
            if device is not None and device.name:
                result = device.name

    if result is None and hasattr(entity, "original_name") and entity.original_name:
        result = entity.original_name

    if result is None and hasattr(entity, "name") and entity.name:
        result = entity.name

    return result


ADD_STATES_FIELD = "add_states"
_UNUSABLE_SOURCE_STATES = {STATE_UNKNOWN, STATE_UNAVAILABLE, ""}


def _cooldown_selector() -> NumberSelector:
    """Return the selector for the default cooldown."""
    return NumberSelector(
        NumberSelectorConfig(
            min=1,
            max=3600,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


def _main_sensor_schema(config_data: dict[str, Any]) -> vol.Schema:
    """
    Return the main settings form: meter, budget, cooldown, sustained load.

    The budget is either one figure or follows a source entity, one budget
    per state of it. The figure stays required in both modes: it is what the
    balancer starts on and keeps while the source reports a state with no
    budget of its own.
    """
    sensor = config_data.get(CONF_MAIN_POWER_SENSOR)
    budget = config_data.get(CONF_POWER_BUDGET_WATT)
    source = config_data.get(CONF_BUDGET_SOURCE_ENTITY)
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_MAIN_POWER_SENSOR,
            **({"default": sensor} if sensor is not None else {}),
        ): _get_power_sensor_selector(),
        vol.Required(
            CONF_BUDGET_MODE,
            default=BUDGET_MODE_SOURCE if source else BUDGET_MODE_FIXED,
        ): SelectSelector(
            SelectSelectorConfig(
                options=list(BUDGET_MODES),
                mode=SelectSelectorMode.LIST,
                translation_key="budget_mode",
            )
        ),
        vol.Required(
            CONF_POWER_BUDGET_WATT,
            **({"default": budget} if budget is not None else {}),
        ): int,
        vol.Optional(
            CONF_BUDGET_SOURCE_ENTITY,
            description={"suggested_value": source},
        ): EntitySelector(
            EntitySelectorConfig(domain=["sensor", "select", "input_select"])
        ),
        vol.Optional(
            CONF_BUDGET_RAISE_DELAY_SECONDS,
            default=config_data.get(
                CONF_BUDGET_RAISE_DELAY_SECONDS, DEFAULT_BUDGET_RAISE_DELAY_SECONDS
            ),
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=3600,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
        vol.Required(
            CONF_COOLDOWN_SECONDS,
            default=config_data.get(CONF_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS),
        ): _cooldown_selector(),
    }
    fields.update(_sustained_schema_fields(config_data))
    return vol.Schema(fields)


def _store_main_sensor_input(
    config_data: dict[str, Any], user_input: dict[str, Any]
) -> dict[str, str]:
    """
    Validate the main settings form and store it; return any errors.

    Choosing a fixed budget forgets the source entity but keeps its per-state
    budgets, so switching back offers them again. Choosing a different source
    entity starts its budgets afresh: the old states mean nothing to it.
    """
    errors: dict[str, str] = {}
    power_budget = user_input.get(CONF_POWER_BUDGET_WATT)
    if not isinstance(power_budget, int) or power_budget <= 0:
        errors[CONF_POWER_BUDGET_WATT] = "valid_budget_required"
    mode = user_input.get(CONF_BUDGET_MODE, BUDGET_MODE_FIXED)
    source = user_input.get(CONF_BUDGET_SOURCE_ENTITY)
    if mode == BUDGET_MODE_SOURCE and not source:
        errors[CONF_BUDGET_SOURCE_ENTITY] = "source_entity_required"
    if errors:
        return errors

    config_data[CONF_MAIN_POWER_SENSOR] = user_input[CONF_MAIN_POWER_SENSOR]
    config_data[CONF_POWER_BUDGET_WATT] = power_budget
    cooldown = user_input.get(CONF_COOLDOWN_SECONDS)
    config_data[CONF_COOLDOWN_SECONDS] = (
        int(cooldown)
        if isinstance(cooldown, (int, float))
        else DEFAULT_COOLDOWN_SECONDS
    )
    _store_sustained_options(config_data, user_input)

    if mode == BUDGET_MODE_SOURCE:
        previous = config_data.get(CONF_BUDGET_SOURCE_ENTITY)
        if previous and previous != source:
            config_data[CONF_BUDGET_SOURCE_MAP] = {}
        config_data[CONF_BUDGET_SOURCE_ENTITY] = source
        config_data[CONF_BUDGET_RAISE_DELAY_SECONDS] = int(
            user_input.get(
                CONF_BUDGET_RAISE_DELAY_SECONDS, DEFAULT_BUDGET_RAISE_DELAY_SECONDS
            )
        )
    else:
        config_data.pop(CONF_BUDGET_SOURCE_ENTITY, None)
    return {}


async def _recorded_states(hass: HomeAssistant, entity_id: str) -> list[str]:
    """
    Return the states the recorder saw an entity report lately, newest first.

    Best effort: without the recorder, or if the query fails, the form still
    offers the declared and current states and lets the user add others.
    """
    if "recorder" not in hass.config.components:
        return []
    try:
        from homeassistant.components.recorder import (  # noqa: PLC0415
            get_instance,
            history,
        )

        start = dt_util.utcnow() - timedelta(days=BUDGET_SOURCE_HISTORY_DAYS)
        changes = await get_instance(hass).async_add_executor_job(
            partial(
                history.state_changes_during_period,
                hass,
                start,
                entity_id=entity_id,
                no_attributes=True,
                descending=True,
                limit=5000,
            )
        )
    except Exception:
        _LOGGER.debug("Could not read the history of %s", entity_id, exc_info=True)
        return []
    return [state.state for state in changes.get(entity_id.lower(), [])]


async def _known_source_states(
    hass: HomeAssistant, entity_id: str, also: list[str]
) -> list[str]:
    """
    Return the states to offer a budget for, in a stable, useful order.

    Declared options first (an enum sensor or a select lists every state it
    can take), then what the recorder saw, then the current state, then any
    already configured or added by hand.
    """
    states: list[str] = []

    def _add(value: object) -> None:
        if (
            isinstance(value, str)
            and value not in _UNUSABLE_SOURCE_STATES
            and value not in states
        ):
            states.append(value)

    current = hass.states.get(entity_id)
    if current is not None:
        for option in current.attributes.get("options") or []:
            _add(option)
    for value in await _recorded_states(hass, entity_id):
        _add(value)
    if current is not None:
        _add(current.state)
    configured = [value for value in also if value not in states]
    room = max(BUDGET_SOURCE_MAX_STATES - len(configured), 0)
    return [*states[:room], *configured]


def _budget_states_schema(states: list[str], prefill: dict[str, Any]) -> vol.Schema:
    """Return one budget field per source state, plus a field to add states."""
    fields: dict[Any, Any] = {
        vol.Optional(
            state, description={"suggested_value": prefill.get(state)}
        ): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=1_000_000,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        )
        for state in states
    }
    fields[vol.Optional(ADD_STATES_FIELD, default=[])] = SelectSelector(
        SelectSelectorConfig(options=[], multiple=True, custom_value=True)
    )
    return vol.Schema(fields)


class _BudgetStatesStep:
    """
    The per-state budget form, shared by the config and options flows.

    Shown after the main settings when the budget follows a source entity.
    A state left blank has no budget of its own: while the source reports
    it, the budget stays where it was.
    """

    hass: HomeAssistant
    _config_data: dict[str, Any]
    _added_source_states: list[str]

    async def _async_return_to_menu(self) -> ConfigFlowResult:
        """Return to the flow's menu once the form is done."""
        raise NotImplementedError

    async def async_step_budget_states(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the budget that goes with each state of the source."""
        entity_id: str = self._config_data[CONF_BUDGET_SOURCE_ENTITY]
        saved: dict[str, int] = dict(
            self._config_data.get(CONF_BUDGET_SOURCE_MAP) or {}
        )
        added = getattr(self, "_added_source_states", [])
        errors: dict[str, str] = {}
        prefill: dict[str, Any] = saved

        if user_input is not None:
            entered = {
                state: int(value)
                for state, value in user_input.items()
                if state != ADD_STATES_FIELD and value not in (None, "")
            }
            new_states = [
                str(value).strip()
                for value in user_input.get(ADD_STATES_FIELD) or []
                if str(value).strip()
            ]
            prefill = entered
            if new_states:
                self._added_source_states = [
                    *added,
                    *(value for value in new_states if value not in added),
                ]
                added = self._added_source_states
            elif not entered:
                errors["base"] = "budget_map_required"
            else:
                self._config_data[CONF_BUDGET_SOURCE_MAP] = entered
                self._added_source_states = []
                return await self._async_return_to_menu()

        states = await _known_source_states(self.hass, entity_id, [*saved, *added])
        source = self.hass.states.get(entity_id)
        name = (
            source.attributes.get("friendly_name", entity_id) if source else entity_id
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="budget_states",
            data_schema=_budget_states_schema(states, prefill),
            errors=errors,
            description_placeholders={
                "source": str(name),
                "budget": str(self._config_data.get(CONF_POWER_BUDGET_WATT)),
            },
        )


def _process_sensor_input(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """
    Process and validate sensor input.

    Args:
        hass: Home Assistant instance.
        user_input: User input dictionary.

    Returns:
        Tuple of (errors dict, processed sensor config or None if errors).

    """
    errors: dict[str, str] = {}

    sensor_entity_id = user_input.get(CONF_ENTITY_ID)
    appliance_entity_id = user_input.get(CONF_APPLIANCE)
    custom_name = user_input.get(CONF_NAME)

    if not sensor_entity_id:
        errors[CONF_ENTITY_ID] = "select_sensor_required"
    if not appliance_entity_id:
        errors[CONF_APPLIANCE] = "select_appliance_required"

    if errors:
        return errors, None

    sensor_entity_id_str = str(sensor_entity_id)
    appliance_entity_id_str = str(appliance_entity_id)

    friendly_name = _get_friendly_name_for_entity(
        hass, appliance_entity_id_str
    ) or _get_friendly_name_for_entity(hass, sensor_entity_id_str)
    if custom_name:
        name_to_use = str(custom_name)
    elif friendly_name:
        name_to_use = friendly_name
    else:
        name_to_use = sensor_entity_id_str

    device_cooldown = user_input.get(CONF_DEVICE_COOLDOWN)
    if device_cooldown is not None and device_cooldown < 0:
        device_cooldown = None

    importance_value = user_input.get(CONF_IMPORTANCE, DEFAULT_IMPORTANCE)
    importance = (
        int(importance_value)
        if isinstance(importance_value, (int, float))
        else DEFAULT_IMPORTANCE
    )

    sensor_config: dict[str, Any] = {
        CONF_ENTITY_ID: sensor_entity_id_str,
        CONF_NAME: name_to_use,
        CONF_IMPORTANCE: importance,
        CONF_LAST_RESORT: bool(user_input.get(CONF_LAST_RESORT, False)),
        CONF_APPLIANCE: appliance_entity_id_str,
    }

    if device_cooldown is not None:
        sensor_config[CONF_DEVICE_COOLDOWN] = int(device_cooldown)

    nominal_power = user_input.get(CONF_NOMINAL_POWER_WATT)
    if isinstance(nominal_power, (int, float)) and nominal_power > 0:
        sensor_config[CONF_NOMINAL_POWER_WATT] = int(nominal_power)

    sensor_config[CONF_POWER_DRAW_MODE] = resolve_power_draw_mode(user_input)
    if (
        sensor_config[CONF_POWER_DRAW_MODE] == POWER_DRAW_FIXED
        and CONF_NOMINAL_POWER_WATT not in sensor_config
    ):
        return {CONF_NOMINAL_POWER_WATT: "fixed_power_required"}, None

    return errors, sensor_config


STEP_ADD_SENSOR_SCHEMA: vol.Schema = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): _get_power_sensor_selector(),
        vol.Optional(CONF_NAME): TextSelector(TextSelectorConfig()),
        vol.Required(CONF_IMPORTANCE, default=DEFAULT_IMPORTANCE): NumberSelector(
            NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
        ),
        vol.Required(CONF_LAST_RESORT, default=False): bool,
        vol.Required(CONF_APPLIANCE): _get_appliance_selector(),
        vol.Required(
            CONF_POWER_DRAW_MODE, default=DEFAULT_POWER_DRAW_MODE
        ): _power_draw_mode_selector(),
        vol.Optional(
            CONF_NOMINAL_POWER_WATT, default=DEFAULT_NOMINAL_POWER_WATT
        ): _nominal_power_selector(),
        vol.Optional(CONF_DEVICE_COOLDOWN, default=-1): NumberSelector(
            NumberSelectorConfig(
                min=-1,
                max=3600,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
    }
)


class PowerLoadBalancerConfigFlow(_BudgetStatesStep, ConfigFlow, domain=DOMAIN):
    """
    Handle a config flow for Power Load Balancer.

    This class manages the initial setup flow for the integration,
    guiding users through main sensor configuration and appliance setup.
    """

    VERSION: int = 1
    MINOR_VERSION: int = 3
    _config_data: dict[str, Any]

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """
        Start with the main settings; appliances are added afterwards.

        Each appliance is its own subentry, added from the integration page
        once the balancer exists, and notifications are set in its options.
        """
        if not hasattr(self, "_config_data"):
            self._config_data = {}
        return await self.async_step_edit_main_sensor(user_input)

    async def _async_create(self) -> ConfigFlowResult:
        """Create the entry from the collected settings."""
        return self.async_create_entry(
            title="Power Load Balancer", data=self._config_data
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,  # noqa: ARG003
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Each appliance the balancer may switch is a subentry of its entry."""
        return {SUBENTRY_APPLIANCE: ApplianceSubentryFlow}

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Provide an options flow handler for editing existing configuration."""
        return PowerLoadBalancerOptionsFlow(config_entry)

    async def async_step_edit_main_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Show form to add or edit the main sensor configuration.

        Args:
            user_input: User input from the form, or None on first display.

        Returns:
            ConfigFlowResult with form or directing to next step.

        """
        _LOGGER.debug(
            "Opening config flow step: async_step_edit_main_sensor, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _store_main_sensor_input(self._config_data, dict(user_input))
            if not errors:
                if self._config_data.get(CONF_BUDGET_SOURCE_ENTITY):
                    return await self.async_step_budget_states()
                return await self._async_create()

        return self.async_show_form(
            step_id="edit_main_sensor",
            data_schema=_main_sensor_schema(self._config_data),
            errors=errors,
        )

    async def _async_return_to_menu(self) -> ConfigFlowResult:
        """Return to the menu after the per-state budget form."""
        return await self._async_create()


class PowerLoadBalancerOptionsFlow(_BudgetStatesStep, OptionsFlow):
    """
    Options flow to reconfigure Power Load Balancer after initial setup.

    It covers the balancer's own settings. Appliances are subentries, edited
    from the integration page.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        """
        Initialize PowerLoadBalancerOptionsFlow.

        Args:
            config_entry: The existing configuration entry to modify.

        """
        self._config_entry = config_entry
        data_source = {
            **config_entry.data,
            **config_entry.options,
        }
        self._config_data: dict[str, Any] = deepcopy(dict(data_source))

    async def async_step_init(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Show the settings menu.

        Appliances are not here: each is a subentry with its own edit and
        delete on the integration page, and its own balancing switch.
        """
        return self.async_show_menu(
            step_id="init",
            menu_options=["edit_main_sensor", "edit_notifications", "finish"],
        )

    async def async_step_finish(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Save the settings.

        The configuration lives in data alone (see async_migrate_entry);
        options stay empty. One update, one reload.
        """
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data=deepcopy(self._config_data),
            options={},
        )
        return self.async_create_entry(title="", data={})

    async def async_step_edit_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Show the notification settings.

        These belong beside the balancer's own settings rather than inside the
        main sensor form: they say nothing about which meter is read or what
        the ceiling is, and burying them under an unrelated heading is why
        they were hard to find.
        """
        errors: dict[str, str] = {}
        prefill = self._config_data
        if user_input is not None:
            errors = _validate_notify_options(user_input)
            if not errors:
                _store_notify_options(self._config_data, user_input)
                return await self.async_step_init()
            # Re-show what was just submitted rather than what was saved, so a
            # rejected form does not throw the choices away.
            prefill = dict(self._config_data)
            _store_notify_options(prefill, user_input)

        return self.async_show_form(
            step_id="edit_notifications",
            data_schema=vol.Schema(_notify_schema_fields(self.hass, prefill)),
            errors=errors,
        )

    async def async_step_edit_main_sensor(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """
        Show form to edit the main sensor and power budget.

        Args:
            user_input: User input from the form, or None on first display.

        Returns:
            ConfigFlowResult with form or directing to next step.

        """
        _LOGGER.debug(
            "Opening options flow step: async_step_edit_main_sensor, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _store_main_sensor_input(self._config_data, dict(user_input))
            if not errors:
                if self._config_data.get(CONF_BUDGET_SOURCE_ENTITY):
                    return await self.async_step_budget_states()
                return await self.async_step_init()

        return self.async_show_form(
            step_id="edit_main_sensor",
            data_schema=_main_sensor_schema(self._config_data),
            errors=errors,
        )

    async def _async_return_to_menu(self) -> ConfigFlowResult:
        """Return to the menu after the per-state budget form."""
        return await self.async_step_init()


class ApplianceSubentryFlow(ConfigSubentryFlow):
    """
    Add or edit one appliance the balancer may switch.

    Each appliance is a subentry, so it is listed on the integration page with
    its own edit and delete instead of behind a dropdown. Its unique id is the
    appliance entity, which keeps one appliance from being added twice.
    Whether it is balanced at all is its switch, not part of this form, so
    that it can be flipped from a dashboard or an automation.
    """

    def _appliance_taken(self, appliance: str, own_id: str | None = None) -> bool:
        """Return True if another subentry already balances this appliance."""
        return any(
            subentry.unique_id == appliance and subentry.subentry_id != own_id
            for subentry in self._get_entry().subentries.values()
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add an appliance."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, appliance = _process_sensor_input(self.hass, user_input)
            if appliance is not None:
                if self._appliance_taken(appliance[CONF_APPLIANCE]):
                    errors[CONF_APPLIANCE] = "appliance_already_configured"
                else:
                    return self.async_create_entry(
                        title=appliance[CONF_NAME],
                        data={**appliance, CONF_BALANCING_ENABLED: True},
                        unique_id=appliance[CONF_APPLIANCE],
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_ADD_SENSOR_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an appliance; its balancing switch keeps its position."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, appliance = _process_sensor_input(self.hass, user_input)
            if appliance is not None:
                if self._appliance_taken(
                    appliance[CONF_APPLIANCE], subentry.subentry_id
                ):
                    errors[CONF_APPLIANCE] = "appliance_already_configured"
                else:
                    return self.async_update_and_abort(
                        self._get_entry(),
                        subentry,
                        title=appliance[CONF_NAME],
                        unique_id=appliance[CONF_APPLIANCE],
                        data={
                            **appliance,
                            CONF_BALANCING_ENABLED: subentry.data.get(
                                CONF_BALANCING_ENABLED, True
                            ),
                        },
                    )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_sensor_edit_schema(
                dict(subentry.data) if user_input is None else user_input
            ),
            errors=errors,
        )
