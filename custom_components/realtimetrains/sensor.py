"""Sensors: N identical departure sensors + one API status sensor."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, STATUS_DEGRADED, STATUS_OK, STATUS_OPTIONS
from .coordinator import RttConfigEntry, RttCoordinator
from .parsing import Departure


async def async_setup_entry(
    hass: HomeAssistant, entry: RttConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    # If the user lowered "max departures", remove the now-surplus sensors.
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_departure_"
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity_entry.unique_id.startswith(prefix):
            index = int(entity_entry.unique_id[len(prefix):])
            if index > coordinator.max_departures:
                registry.async_remove(entity_entry.entity_id)

    entities: list[SensorEntity] = [
        RttDepartureSensor(coordinator, entry, index)
        for index in range(1, coordinator.max_departures + 1)
    ]
    entities.append(RttApiStatusSensor(coordinator, entry))
    async_add_entities(entities)


def _round_tokens(value: float | None) -> int | None:
    """Governor tokens = calls we can still spend now without exceeding the budget."""
    return None if value is None else round(value)


class _RttEntity(CoordinatorEntity[RttCoordinator], SensorEntity):
    """Shared plumbing: all entities hang off one device named after the station."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: RttCoordinator, entry: RttConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.station_name,
            manufacturer="Realtime Trains",
            entry_type=DeviceEntryType.SERVICE,
        )


class RttDepartureSensor(_RttEntity):
    """"<Home Station> Departures N": the Nth upcoming departure.

    All instances are the same class - only the index differs.

    STATE      = expected departure time (a timestamp). Sortable, works with
                 HA's relative-time display and time triggers, and is the one
                 thing you'd want to see at a glance. `unknown` when there is
                 no Nth train.
    ATTRIBUTES = everything else (see parsing.build_departure).
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:train"

    # Keep the bulky / fast-changing attributes out of the recorder database.
    # They still appear live on the entity and in templates/cards, but are not
    # written to history every time a forecast moves. (This also avoids the
    # recorder's 16 KB per-state attribute limit on long routes.)
    _unrecorded_attributes = frozenset({"calling_points", "calling_at", "reasons", "units", "facilities"})

    def __init__(self, coordinator: RttCoordinator, entry: RttConfigEntry, index: int) -> None:
        super().__init__(coordinator, entry)
        self._index = index  # 1-based, as in the sensor name
        self._attr_name = f"Departures {index}"
        self._attr_unique_id = f"{entry.entry_id}_departure_{index}"

    @property
    def _departure(self) -> Departure | None:
        data = self.coordinator.data
        if data is None or self._index > len(data.departures):
            return None
        return data.departures[self._index - 1]

    @property
    def native_value(self) -> datetime | None:
        departure = self._departure
        return departure.departure_time if departure else None   # None -> "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        departure = self._departure
        return dict(departure.attributes) if departure else {}


class RttApiStatusSensor(_RttEntity):
    """API health.

    STATE      = ok | degraded | rate_limited | auth_error | error
    ATTRIBUTES = api_success, calls remaining per period, limits, timestamps...
    """

    _attr_name = "API status"
    _attr_icon = "mdi:api"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RttCoordinator, entry: RttConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_api_status"

    @property
    def available(self) -> bool:
        # Unlike the departure sensors this must stay available when the API is
        # failing - reporting the failure is its whole job.
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.client.status.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.coordinator.client.status
        data = self.coordinator.data
        attrs: dict[str, Any] = {
            "api_success": status.state in (STATUS_OK, STATUS_DEGRADED),
            "last_attempt": status.last_attempt.isoformat() if status.last_attempt else None,
            "last_success": status.last_success.isoformat() if status.last_success else None,
            "last_error": status.last_error,
            "api_calls_since_start": status.calls_made,
            "api_calls_last_update": data.calls_in_update if data else None,
            "poll_interval_seconds": (
                int(self.coordinator.update_interval.total_seconds())
                if self.coordinator.update_interval
                else None
            ),
            "rtt_network_rail_status": status.system_status.get("realtimeNetworkRail"),
            "rtt_core_status": status.system_status.get("rttCore"),
            "budget_delay_seconds": round(self.coordinator.budget_delay),
            "budget_calls_available_hour": _round_tokens(self.coordinator.budget.tokens.get("Hour")),
            "budget_calls_available_day": _round_tokens(self.coordinator.budget.tokens.get("Day")),
            "token_valid_until": status.token_valid_until.isoformat() if status.token_valid_until else None,
            "entitlements": status.entitlements,
        }
        # calls_remaining_minute / _hour / _day / _week (+ the matching limits),
        # for whichever periods RTT reports for your key.
        for dim, remaining in status.remaining.items():
            attrs[f"calls_remaining_{dim.lower()}"] = remaining
        for dim, limit in status.limits.items():
            attrs[f"call_limit_{dim.lower()}"] = limit
        return attrs
