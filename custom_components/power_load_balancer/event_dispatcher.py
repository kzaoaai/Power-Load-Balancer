"""
Event fan-out for the Power Load Balancer integration.

Every balancer event message flows through a single dispatcher so the
event-log sensor, the event bus, and the optional notification channels
always stay in sync.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.exceptions import ServiceNotFound

from .const import (
    CHANNEL_BOTH,
    CHANNEL_NONE,
    CHANNEL_PERSISTENT,
    CHANNEL_SERVICE,
    DOMAIN,
    EVENT_POWER_LOAD_BALANCER,
    NOTIFY_PROBLEM,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

NOTIFICATION_TITLE = "Power Load Balancer"


class BalancerEventDispatcher:
    """
    Fans out balancer events to every configured consumer.

    Consumers are: the event-log sensor (once registered), a
    power_load_balancer_event on the event bus, an optional Home Assistant
    persistent notification (a fixed notification_id, so repeated events
    update one card instead of accumulating), and an optional notify
    service such as a mobile app notifier. Notification failures are
    logged and swallowed — notifying must never break balancing.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        notify_services: list[str],
        channels: dict[str, str],
    ) -> None:
        """
        Initialize the dispatcher.

        Args:
            hass: Home Assistant instance.
            entry_id: Config entry id, included in bus events and used for
                the persistent notification id.
            notify_services: Service names under the notify domain to send
                events to. Empty disables pushing entirely.
            channels: Delivery channel per event category, as returned by
                resolve_notify_channels. The event log and the bus event
                always receive everything regardless.

        """
        self.hass = hass
        self._entry_id = entry_id
        self._notify_services = list(notify_services)
        self._channels = dict(channels)
        self._sensor: Any = None
        # Tracked per service: one unreachable notifier must not suppress the
        # warning for a different one that breaks later.
        self._notify_failure_logged: set[str] = set()

    def set_sensor(self, sensor: Any) -> None:
        """Register the event-log sensor that should receive events."""
        self._sensor = sensor

    @callback
    def add_log_entry(self, message: str, category: str = NOTIFY_PROBLEM) -> None:
        """
        Record one balancer event and fan it out to all consumers.

        The event log and the bus event always receive everything: they are
        the record, and a record with holes in it is worse than none at all
        when working out afterwards what the balancer did. The category only
        decides which notification channel, if any, this event is worth
        interrupting someone through.

        Keeps the add_log_entry name so existing callers can treat the
        dispatcher exactly like the event-log sensor it wraps.
        """
        if self._sensor is not None:
            self._sensor.add_log_entry(message)

        self.hass.bus.async_fire(
            EVENT_POWER_LOAD_BALANCER,
            {
                "entry_id": self._entry_id,
                "message": message,
                "category": category,
            },
        )

        channel = self._channels.get(category, CHANNEL_NONE)

        if channel in (CHANNEL_PERSISTENT, CHANNEL_BOTH):
            try:
                persistent_notification.async_create(
                    self.hass,
                    message,
                    title=NOTIFICATION_TITLE,
                    notification_id=f"{DOMAIN}_{self._entry_id}",
                )
            except Exception:
                _LOGGER.exception("Failed to create persistent notification")

        if channel in (CHANNEL_SERVICE, CHANNEL_BOTH):
            for service in self._notify_services:
                self.hass.async_create_task(self._async_send_notify(service, message))

    async def _async_send_notify(self, service: str, message: str) -> None:
        """Send one event message through one configured notify service."""
        try:
            await self.hass.services.async_call(
                "notify",
                service,
                {"title": NOTIFICATION_TITLE, "message": message},
                blocking=False,
            )
        except ServiceNotFound:
            if service not in self._notify_failure_logged:
                _LOGGER.warning(
                    "notify.%s is not available; balancer notifications to it "
                    "are dropped until the service appears (logged once)",
                    service,
                )
                self._notify_failure_logged.add(service)
        except Exception:
            if service not in self._notify_failure_logged:
                _LOGGER.exception(
                    "Failed to send notification via notify.%s "
                    "(this is logged once until a send succeeds)",
                    service,
                )
                self._notify_failure_logged.add(service)
        else:
            if service in self._notify_failure_logged:
                _LOGGER.info(
                    "notify.%s is available again; balancer notifications resumed",
                    service,
                )
                self._notify_failure_logged.discard(service)

    @callback
    def async_dismiss_persistent(self) -> None:
        """Dismiss this entry's persistent notification card, if any."""
        try:
            persistent_notification.async_dismiss(
                self.hass, f"{DOMAIN}_{self._entry_id}"
            )
        except Exception:
            _LOGGER.exception("Failed to dismiss persistent notification")

    def get_diagnostics_snapshot(self) -> dict[str, Any]:
        """Return notification configuration for diagnostics."""
        return {
            "notify_services": list(self._notify_services),
            "notify_channels": dict(self._channels),
            "sensor_registered": self._sensor is not None,
        }
