"""
Shared fixtures for the Power Load Balancer tests.

The balancer switches real contactors on real tanks, so the scenarios that
matter most here are the ones that would be expensive to discover on the
hardware: a shed that never gets released, a shed/restore pair that chatters
a relay, or a stale power reading that makes the balancer shed a rung it did
not need to.

Everything runs against a real Home Assistant instance from
pytest-homeassistant-custom-component, so entity/service registries, the
event loop and the state machine behave as they do in production.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_load_balancer import appliance_controller, power_balancer
from custom_components.power_load_balancer.const import DOMAIN

MAIN_SENSOR = "sensor.inverter_load_apparent_power"

UPSTAIRS = "water_heater.upstairs_tank"
UPSTAIRS_POWER = "sensor.upstairs_tank_power"
DOWNSTAIRS = "water_heater.downstairs_tank"
DOWNSTAIRS_POWER = "sensor.downstairs_tank_power"
HEATER = "switch.spare_heater"
HEATER_POWER = "sensor.spare_heater_power"

TANK_VA = 2050
BUDGET = 8000
THRESHOLD = 78
DWELL = 60
ARM_LEVEL = BUDGET * THRESHOLD // 100  # 6240


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""
    return


class ServiceRecorder:
    """Records the service calls the balancer makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def register(self, hass: HomeAssistant, domain: str, service: str) -> None:
        async def _handle(call: ServiceCall) -> None:
            entity_id = call.data.get("entity_id")
            if isinstance(entity_id, list):
                entity_id = entity_id[0] if entity_id else None
            self.calls.append((domain, service, entity_id))

        hass.services.async_register(domain, service, _handle)

    def of(self, entity_id: str) -> list[tuple[str, str]]:
        """Return (domain, service) pairs recorded for one entity."""
        return [(d, s) for d, s, e in self.calls if e == entity_id]

    def services_for(self, entity_id: str) -> list[str]:
        """Return just the service names recorded for one entity."""
        return [s for _, s, e in self.calls if e == entity_id]

    def count(self, entity_id: str, service: str) -> int:
        return sum(1 for _, s, e in self.calls if e == entity_id and s == service)

    def clear(self) -> None:
        self.calls.clear()


@pytest.fixture
def recorder() -> ServiceRecorder:
    return ServiceRecorder()


class Clock:
    """
    A controllable stand-in for the integration's monotonic clock.

    Durations (dwell, cooldowns, shed age) are read through
    power_load_balancer.clock.monotonic, so replacing that one function lets a
    test hold a load "for a minute" without waiting one -- and without
    touching the event loop's own clock, which asyncio needs for scheduling.
    """

    def __init__(self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
        self._hass = hass
        self._now = 10_000.0
        for module in (power_balancer, appliance_controller):
            monkeypatch.setattr(module, "monotonic", lambda: self._now)

    @property
    def now(self) -> float:
        return self._now

    async def advance(self, seconds: float) -> None:
        """Move time forward and let anything waiting on it run."""
        self._now += seconds
        await self._hass.async_block_till_done()


@pytest.fixture
def clock(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(hass, monkeypatch)


@pytest.fixture
def world(hass: HomeAssistant, recorder: ServiceRecorder):
    """Build a house: a main meter, two tanks and a plain switch, all idle."""
    hass.states.async_set(
        MAIN_SENSOR,
        "3000",
        {"unit_of_measurement": "VA", "device_class": "apparent_power"},
    )
    for power_sensor in (UPSTAIRS_POWER, DOWNSTAIRS_POWER, HEATER_POWER):
        hass.states.async_set(power_sensor, "0", {"unit_of_measurement": "W"})

    hass.states.async_set(
        UPSTAIRS, "off", {"operation_list": ["electric", "off", "performance"]}
    )
    hass.states.async_set(
        DOWNSTAIRS, "off", {"operation_list": ["electric", "off", "performance"]}
    )
    hass.states.async_set(HEATER, "off", {})

    for service in ("turn_on", "turn_off", "set_operation_mode"):
        recorder.register(hass, "water_heater", service)
    for service in ("turn_on", "turn_off"):
        recorder.register(hass, "switch", service)

    return recorder


@pytest.fixture
def register_shed_platform(hass: HomeAssistant, recorder: ServiceRecorder):
    """
    Make the tanks look like generic_water_heater entities with shed support.

    The real integration keeps the operation mode untouched while shed and
    flags it with a load_shed attribute, so the entity keeps reporting an
    active-looking state. That detail is the whole point of these tests.
    """

    def _register(*entity_ids: str, with_services: bool = True) -> None:
        registry = er.async_get(hass)
        for index, entity_id in enumerate(entity_ids):
            domain, object_id = entity_id.split(".", 1)
            # async_get_or_create refuses an entity_id the state machine
            # already holds and silently suffixes it, so free the id first
            # and put the state back afterwards.
            existing = hass.states.get(entity_id)
            if existing is not None:
                hass.states.async_remove(entity_id)
            registry.async_get_or_create(
                domain,
                "generic_water_heater",
                f"shedtest_{index}_{object_id}",
                suggested_object_id=object_id,
            )
            assert registry.async_get(entity_id) is not None, (
                f"fixture failed to register {entity_id}"
            )
            if existing is not None:
                hass.states.async_set(
                    entity_id, existing.state, dict(existing.attributes)
                )

        if not with_services:
            return

        async def _shed(call: ServiceCall) -> None:
            for entity_id in _targets(call):
                recorder.calls.append(("generic_water_heater", "shed", entity_id))
                state = hass.states.get(entity_id)
                attributes = dict(state.attributes) if state else {}
                attributes["load_shed"] = True
                hass.states.async_set(
                    entity_id, state.state if state else "electric", attributes
                )

        async def _release(call: ServiceCall) -> None:
            for entity_id in _targets(call):
                recorder.calls.append(("generic_water_heater", "release", entity_id))
                state = hass.states.get(entity_id)
                attributes = dict(state.attributes) if state else {}
                attributes["load_shed"] = False
                hass.states.async_set(
                    entity_id, state.state if state else "electric", attributes
                )

        hass.services.async_register("generic_water_heater", "shed", _shed)
        hass.services.async_register("generic_water_heater", "release", _release)

    def _targets(call: ServiceCall) -> list[str]:
        entity_id = call.data.get("entity_id")
        if isinstance(entity_id, str):
            return [entity_id]
        return list(entity_id or [])

    return _register


def ladder(*, include_heater: bool = False) -> list[dict]:
    """Return the appliance ladder: upstairs sheds first, then downstairs."""
    rungs = [
        {
            "appliance": UPSTAIRS,
            "entity_id": UPSTAIRS_POWER,
            "name": "Upstairs Tank",
            "importance": 1,
            "last_resort": False,
            "device_cooldown": 180,
        },
        {
            "appliance": DOWNSTAIRS,
            "entity_id": DOWNSTAIRS_POWER,
            "name": "Downstairs Tank",
            "importance": 2,
            "last_resort": False,
            "device_cooldown": 180,
        },
    ]
    if include_heater:
        rungs.append(
            {
                "appliance": HEATER,
                "entity_id": HEATER_POWER,
                "name": "Spare Heater",
                "importance": 3,
                "last_resort": False,
            }
        )
    return rungs


@pytest.fixture
def setup_balancer(hass: HomeAssistant) -> Callable:
    """Set up the integration with a given options payload."""

    async def _setup(**overrides: object) -> MockConfigEntry:
        options = {
            "main_power_sensor": MAIN_SENSOR,
            "power_budget_watt": BUDGET,
            "cooldown_seconds": 120,
            "sustained_shedding_enabled": True,
            "sustained_threshold_percent": THRESHOLD,
            "sustained_duration_seconds": DWELL,
            "power_sensors": ladder(),
        }
        options.update(overrides)

        entry = MockConfigEntry(domain=DOMAIN, data=options, options=options)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        return entry

    return _setup


def balancer_of(hass: HomeAssistant, entry: MockConfigEntry):
    """Return the running PowerLoadBalancer for an entry."""
    return hass.data[DOMAIN][entry.entry_id]


async def feed(hass: HomeAssistant, value: float, *, sensor: str = MAIN_SENSOR) -> None:
    """Publish one main-meter reading and let the balancer react."""
    hass.states.async_set(
        sensor,
        str(value),
        {"unit_of_measurement": "VA", "device_class": "apparent_power"},
    )
    await hass.async_block_till_done()


async def hold(
    hass: HomeAssistant,
    value: float,
    seconds: float,
    clock=None,
    *,
    interval: float = 10,
) -> None:
    """
    Hold a load for a period, publishing a reading every interval seconds.

    Mirrors a real meter: the balancer only re-evaluates when a reading
    arrives or a scheduled check fires, so a test that jumps time without
    publishing anything is not testing the same code path.
    """
    elapsed = 0.0
    sample = 0
    await feed(hass, value)
    while elapsed < seconds:
        step = min(interval, seconds - elapsed)
        if clock is not None:
            await clock.advance(step)
        elapsed += step
        sample += 1
        # Home Assistant drops a state write that repeats the previous value,
        # so an unchanging feed would deliver no events at all. Real meters
        # jitter; mirror that with a small alternation.
        await feed(hass, value + (sample % 2))
