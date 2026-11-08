"""Constants for the Power Load Balancer integration."""

DOMAIN = "power_load_balancer"
ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE = "device_unavailable"
ISSUE_TRANSLATION_KEY_DEVICE_NOT_CONTROLLABLE = "device_not_controllable"
ISSUE_TRANSLATION_KEY_ENTITY_NOT_REGISTERED = "entity_not_registered"

# A restart makes every entity briefly unavailable, and some integrations take
# minutes to come back. Raising on the first miss turns an ordinary restart
# into a repair per balanced entity, so an outage has to persist before it
# counts as one.
UNAVAILABLE_DEBOUNCE_SECONDS = 300

# Availability is re-judged on a sweep rather than on state edges alone: a
# dead entity sends no event to say it is still dead, and an entity that came
# back while the listener was still being registered would otherwise stay
# flagged forever.
ENTITY_HEALTH_SWEEP_SECONDS = 60

CONF_MAIN_POWER_SENSOR = "main_power_sensor"
CONF_POWER_SENSORS = "power_sensors"
CONF_POWER_BUDGET_WATT = "power_budget_watt"
CONF_APPLIANCE = "appliance"
CONF_IMPORTANCE = "importance"
CONF_LAST_RESORT = "last_resort"
CONF_COOLDOWN_SECONDS = "cooldown_seconds"
CONF_DEVICE_COOLDOWN = "device_cooldown"
CONF_NOMINAL_POWER_WATT = "nominal_power_watt"

DEFAULT_NOMINAL_POWER_WATT = 0

SERVICE_TURN_OFF_APPLIANCE = "turn_off_appliance"
SERVICE_TURN_ON_APPLIANCE = "turn_on_appliance"

DEFAULT_COOLDOWN_SECONDS = 10

CONF_SUSTAINED_ENABLED = "sustained_shedding_enabled"
CONF_SUSTAINED_THRESHOLD_PERCENT = "sustained_threshold_percent"
CONF_SUSTAINED_DURATION_SECONDS = "sustained_duration_seconds"

DEFAULT_SUSTAINED_ENABLED = False
DEFAULT_SUSTAINED_THRESHOLD_PERCENT = 80
DEFAULT_SUSTAINED_DURATION_SECONDS = 60

SUSTAINED_ESCALATION_WINDOW_SECONDS = 180
SUSTAINED_ESCALATION_DWELL_SECONDS = 15
SUSTAINED_DISARM_GRACE_SECONDS = 15
SHED_VERIFY_GRACE_SECONDS = 45
POWER_TICK_SWEEP_SECONDS = 10

# A main meter silent this long has stopped reporting even if it still shows
# a number; balancing pauses rather than act on a house it cannot see.
METER_STALE_SECONDS = 300

# An appliance adopted at startup was shed before this process began, so its
# power sensor reads zero and there is no way to size what restoring it costs.
# Restoring it only in a demonstrably quiet house bounds that uncertainty
# without leaving it suppressed forever.
RESTORE_UNKNOWN_SIZE_FRACTION = 0.5

# Headroom kept back when restoring an appliance of known size, as a share of
# the effective budget. Restoring onto the exact ceiling would arm the
# sustained-load dwell at once, and meter noise would finish the job.
RESTORE_MARGIN_FRACTION = 0.02

# How long after the balancer starts restoring an appliance its move back to
# drawing still counts as that restore. The state change reaches the turn-on
# handler like anybody else's; inside this window it is the balancer's own
# action and must not be judged by the turn-on veto meant for people.
OWN_RESTORE_GRACE_SECONDS = 30

# What the balancer has shed, and the modes it must give back, survive a
# restart or a reload of the entry in this store. Without it a climate or a
# switch shed just before either stayed off: unlike a managed shed, plain
# "off" says nothing about who turned it off.
SHED_STORE_VERSION = 1
SHED_STORE_SAVE_DELAY_SECONDS = 1


def shed_store_key(entry_id: str) -> str:
    """Return the storage key of one entry's shed record."""
    return f"power_load_balancer.{entry_id}.shed"


CONF_NOTIFY_SERVICES = "notify_services"

# The budget can follow a sensor that names the supply carrying the house
# (for instance solar, utility, generator), each supply with its own budget.
# Stored as {state: watts}. A drop applies at once, because the new supply
# may already be overloaded; a rise waits BUDGET_RAISE_DELAY so a supply that
# flickers back for a moment does not restore appliances onto it.
CONF_BUDGET_SOURCE_ENTITY = "budget_source_entity"
CONF_BUDGET_SOURCE_MAP = "budget_source_map"
CONF_BUDGET_RAISE_DELAY_SECONDS = "budget_raise_delay_seconds"
DEFAULT_BUDGET_RAISE_DELAY_SECONDS = 60

# Read-only now: entries saved before delivery became a per-category choice
# still carry these, and notify_channels.resolve_notify_channels translates
# them. Nothing writes them any more -- do not offer them in the config flow.
CONF_NOTIFY_PERSISTENT = "notify_persistent"
CONF_NOTIFY_CATEGORIES = "notify_categories"
CONF_NOTIFY_SERVICE = "notify_service"
DEFAULT_NOTIFY_PERSISTENT = False
DEFAULT_NOTIFY_SERVICE = ""

# What a balancer event is about. Every event is always recorded and put on
# the bus; these only decide which ones are worth interrupting someone for.
NOTIFY_SHED = "shed"
NOTIFY_RESTORE = "restore"
NOTIFY_BUDGET = "budget"
NOTIFY_PROBLEM = "problem"

# Order matters: it is the order of the rows on the settings page.
NOTIFY_CATEGORIES = (NOTIFY_SHED, NOTIFY_RESTORE, NOTIFY_BUDGET, NOTIFY_PROBLEM)

# Where each category is delivered. Chosen per category, because the answer
# is rarely the same for a shed and for a budget change.
CONF_NOTIFY_CHANNELS = "notify_channels"

CHANNEL_NONE = "none"
CHANNEL_PERSISTENT = "persistent"
CHANNEL_SERVICE = "service"
CHANNEL_BOTH = "both"

NOTIFY_CHANNELS = (CHANNEL_NONE, CHANNEL_PERSISTENT, CHANNEL_SERVICE, CHANNEL_BOTH)

EVENT_POWER_LOAD_BALANCER = "power_load_balancer_event"

SHED_AWARE_PLATFORMS: dict[str, dict[str, str]] = {
    "generic_water_heater": {
        "domain": "generic_water_heater",
        "shed": "shed",
        "release": "release",
        "shed_attribute": "load_shed",
    },
}

SUPPORTED_APPLIANCE_DOMAINS = (
    "switch",
    "light",
    "climate",
    "media_player",
    "water_heater",
)
NON_BINARY_ACTIVE_STATE_DOMAINS = ("climate", "media_player", "water_heater")

ATTR_ENTITY_ID = "entity_id"
ATTR_REASON = "reason"

DEVICE_MANUFACTURER = "Power Load Balancer"
DEVICE_MODEL = "Power Load Balancer"


def appliance_control_issue_id(entity_id: str) -> str:
    """Return the Repairs id raised when controlling an appliance finds it gone."""
    return f"{entity_id}_unavailable"
