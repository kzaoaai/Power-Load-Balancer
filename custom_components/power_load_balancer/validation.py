"""
Validation utilities for the Power Load Balancer integration.

This module provides validation functions for entities and power values.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .exceptions import (
    EntityNotFoundError,
    EntityUnavailableError,
    PowerSensorError,
    ValidationError,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

_LOGGER = logging.getLogger(__name__)


def validate_entity_id(entity_id: str) -> None:
    """
    Validate entity ID format.

    Args:
        entity_id: The entity ID to validate.

    Raises:
        ValidationError: If entity ID is invalid.

    """
    if not entity_id or not isinstance(entity_id, str):
        raise ValidationError(
            message="Entity ID must be a non-empty string",
            details={"entity_id": entity_id},
        )

    if "." not in entity_id:
        raise ValidationError(
            message=(
                "Entity ID must contain a domain and entity name separated by a dot"
            ),
            details={"entity_id": entity_id},
        )


def validate_power_value(value: Any, entity_id: str) -> float:
    """
    Validate and convert power value to float.

    Args:
        value: The power value to validate.
        entity_id: The entity ID for error context.

    Returns:
        The validated power value as float.

    Raises:
        PowerSensorError: If value is invalid or negative.

    """
    if value is None:
        raise PowerSensorError(
            message=f"Power value is None for entity {entity_id}",
            details={"entity_id": entity_id, "value": value},
        )

    try:
        power = float(value)
        if power < 0:
            raise PowerSensorError(
                message=f"Power value cannot be negative for entity {entity_id}",
                details={"entity_id": entity_id, "value": power},
            )
    except (ValueError, TypeError) as exc:
        raise PowerSensorError(
            message=f"Cannot convert power value to float for entity {entity_id}",
            details={"entity_id": entity_id, "value": value, "error": str(exc)},
        ) from exc
    else:
        return power


def validate_entity_state(hass: HomeAssistant, entity_id: str) -> State:
    """
    Validate that entity exists and has a valid state.

    Args:
        hass: Home Assistant instance.
        entity_id: The entity ID to validate.

    Returns:
        The entity state object.

    Raises:
        EntityNotFoundError: If entity does not exist.
        EntityUnavailableError: If entity is unavailable.

    """
    validate_entity_id(entity_id)

    state = hass.states.get(entity_id)
    if state is None:
        raise EntityNotFoundError(
            message=f"Entity {entity_id} not found", details={"entity_id": entity_id}
        )

    if state.state in ("unknown", "unavailable"):
        raise EntityUnavailableError(
            message=f"Entity {entity_id} is {state.state}",
            details={"entity_id": entity_id, "state": state.state},
        )

    return state


def convert_power_to_watts(power: float, state: State) -> float:
    """
    Convert power value to watts based on unit of measurement.

    Apparent power units (VA, kVA) are accepted and scaled numerically the
    same way as their active power counterparts. The balancer is agnostic to
    the physical distinction: budgets are interpreted in the same unit family
    as the main power sensor, so a VA main sensor simply means VA budgets.

    Args:
        power: The power value to convert.
        state: The entity state containing unit_of_measurement attribute.

    Returns:
        Power value in watts (or volt-amperes for apparent power sensors).

    """
    unit_raw = state.attributes.get("unit_of_measurement", "W")
    if unit_raw in ("mW", "mVA"):
        return power / 1000

    factors = {
        0.001: ("milliwatt", "milliwatts", "millivolt-ampere"),
        1: ("w", "watt", "watts", "va", "volt-ampere", "voltampere"),
        1000: ("kw", "kilowatt", "kilowatts", "kva", "kilovolt-ampere"),
        1000000: ("mw", "megawatt", "megawatts"),
        1000000000: ("gw", "gigawatt", "gigawatts"),
    }
    unit = unit_raw.lower()
    for factor, units in factors.items():
        if unit in units:
            return power * factor
    _LOGGER.warning(
        "Unknown power unit '%s' for entity %s, assuming watts",
        unit,
        state.entity_id,
    )
    return power
