"""Constants for the Power Load Balancer integration."""

DOMAIN = "power_load_balancer"
ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE = "device_unavailable"
ISSUE_TRANSLATION_KEY_DEVICE_NOT_CONTROLLABLE = "device_not_controllable"

CONF_MAIN_POWER_SENSOR = "main_power_sensor"
CONF_POWER_SENSORS = "power_sensors"
CONF_POWER_BUDGET_WATT = "power_budget_watt"
CONF_APPLIANCE = "appliance"
CONF_IMPORTANCE = "importance"
CONF_LAST_RESORT = "last_resort"
CONF_COOLDOWN_SECONDS = "cooldown_seconds"
CONF_DEVICE_COOLDOWN = "device_cooldown"

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

CONF_NOTIFY_PERSISTENT = "notify_persistent"
CONF_NOTIFY_SERVICE = "notify_service"

DEFAULT_NOTIFY_PERSISTENT = False
DEFAULT_NOTIFY_SERVICE = ""

EVENT_POWER_LOAD_BALANCER = "power_load_balancer_event"

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
