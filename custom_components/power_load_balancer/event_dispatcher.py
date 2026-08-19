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

from .const import DOMAIN, EVENT_POWER_LOAD_BALANCER

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
        notify_persistent: bool,
        notify_service: str,
    ) -> None:
        """
        Initialize the dispatcher.

        Args:
            hass: Home Assistant instance.
            entry_id: Config entry id, included in bus events and used for
                the persistent notification id.
            notify_persistent: Whether to raise a persistent notification
                for each event.
            notify_service: Service name under the notify domain to send
                events to, or an empty string to disable.

        """
        self.hass = hass
        self._entry_id = entry_id
        self._notify_persistent = notify_persistent
        self._notify_service = notify_service
        self._sensor: Any = None
        self._notify_failure_logged = False

    def set_sensor(self, sensor: Any) -> None:
        """Register the event-log sensor that should receive events."""
        self._sensor = sensor

    @callback
    def add_log_entry(self, message: str) -> None:
        """
        Record one balancer event and fan it out to all consumers.

        Keeps the add_log_entry name so existing callers can treat the
        dispatcher exactly like the event-log sensor it wraps.
        """
        if self._sensor is not None:
            self._sensor.add_log_entry(message)

        self.hass.bus.async_fire(
            EVENT_POWER_LOAD_BALANCER,
            {"entry_id": self._entry_id, "message": message},
        )

        if self._notify_persistent:
            try:
                persistent_notification.async_create(
                    self.hass,
                    message,
                    title=NOTIFICATION_TITLE,
                    notification_id=f"{DOMAIN}_{self._entry_id}",
                )
            except Exception:
                _LOGGER.exception("Failed to create persistent notification")

        if self._notify_service:
            self.hass.async_create_task(self._async_send_notify(message))

    async def _async_send_notify(self, message: str) -> None:
        """Send one event message through the configured notify service."""
        try:
            await self.hass.services.async_call(
                "notify",
                self._notify_service,
                {"title": NOTIFICATION_TITLE, "message": message},
                blocking=False,
            )
        except ServiceNotFound:
            if not self._notify_failure_logged:
                _LOGGER.warning(
                    "notify.%s is not available; balancer notifications are "
                    "dropped until the service appears (this is logged once)",
                    self._notify_service,
                )
                self._notify_failure_logged = True
        except Exception:
            if not self._notify_failure_logged:
                _LOGGER.exception(
                    "Failed to send notification via notify.%s "
                    "(this is logged once until a send succeeds)",
                    self._notify_service,
                )
                self._notify_failure_logged = True
        else:
            if self._notify_failure_logged:
                _LOGGER.info(
                    "notify.%s is available again; balancer notifications resumed",
                    self._notify_service,
                )
            self._notify_failure_logged = False

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
            "notify_persistent": self._notify_persistent,
            "notify_service": self._notify_service or None,
            "sensor_registered": self._sensor is not None,
        }
