"""
Where each kind of balancer event gets delivered.

Delivery is chosen per category rather than globally: a shed is worth a push
to a phone, a budget change usually is not, and a problem is worth both. This
module holds the mapping and the translation of older, coarser settings into
it, so the config flow and the dispatcher cannot disagree about what is in
force.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CHANNEL_BOTH,
    CHANNEL_NONE,
    CHANNEL_PERSISTENT,
    CHANNEL_SERVICE,
    CONF_NOTIFY_CATEGORIES,
    CONF_NOTIFY_CHANNELS,
    CONF_NOTIFY_PERSISTENT,
    CONF_NOTIFY_SERVICE,
    DEFAULT_NOTIFY_PERSISTENT,
    DEFAULT_NOTIFY_SERVICE,
    NOTIFY_CATEGORIES,
    NOTIFY_CHANNELS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


def resolve_notify_channels(config_data: Mapping[str, Any]) -> dict[str, str]:
    """
    Return the delivery channel for every event category.

    Entries saved before delivery became a per-category choice carry a single
    persistent-notification switch and a list of selected categories instead.
    Those are translated here rather than by a migration, so the settings page
    opens showing exactly what is in force and writing the new shape back the
    first time it is saved.
    """
    stored = config_data.get(CONF_NOTIFY_CHANNELS)
    if isinstance(stored, dict):
        return {
            category: (
                str(stored.get(category, CHANNEL_NONE))
                if str(stored.get(category, CHANNEL_NONE)) in NOTIFY_CHANNELS
                else CHANNEL_NONE
            )
            for category in NOTIFY_CATEGORIES
        }

    persistent = bool(
        config_data.get(CONF_NOTIFY_PERSISTENT, DEFAULT_NOTIFY_PERSISTENT)
    )
    configured = config_data.get(CONF_NOTIFY_SERVICE, DEFAULT_NOTIFY_SERVICE)
    service = bool(isinstance(configured, str) and configured.strip())
    if persistent and service:
        legacy_channel = CHANNEL_BOTH
    elif persistent:
        legacy_channel = CHANNEL_PERSISTENT
    elif service:
        legacy_channel = CHANNEL_SERVICE
    else:
        legacy_channel = CHANNEL_NONE

    # No category list at all means an entry written before categories
    # existed, and that code notified about everything. Narrowing it here
    # would take two kinds of notification away from someone who never chose
    # to lose them -- and would do it silently, which is the failure mode
    # this whole feature is meant to prevent. An empty list, by contrast, is
    # a deliberate "tell me nothing" and is honoured as such.
    selected = config_data.get(CONF_NOTIFY_CATEGORIES)
    if not isinstance(selected, list):
        selected = list(NOTIFY_CATEGORIES)
    return {
        category: legacy_channel if category in selected else CHANNEL_NONE
        for category in NOTIFY_CATEGORIES
    }
