"""Pure parsing helpers: RTT JSON -> the data our sensors expose.

Deliberately has NO Home Assistant imports so it can be unit-tested on its own.

Terminology
-----------
* "location line-up" = one item from the /gb-nr/location response (a train as
  seen at the home station).
* "service" = the /gb-nr/service response for that train (its whole journey).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# RTT says: a datetime with no offset means "local time of the queried location".
UK_TZ = ZoneInfo("Europe/London")

# displayAs values that mean "this train does not depart from here"
# (null is defined by the spec to mean PASS).
_NON_DEPARTURE_DISPLAY = {None, "PASS", "TERMINATES"}
# Call types where passengers cannot board here.
_NON_BOARDABLE_CALL_TYPES = {"OPERATIONAL_ONLY", "ADVERTISED_SET_DOWN"}


@dataclass(frozen=True)
class Departure:
    """One departure, ready to be shown by a sensor."""

    departure_time: datetime | None   # -> sensor state (expected departure time)
    attributes: dict[str, Any]        # -> sensor attributes


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
_STATION_SUFFIX_RE = re.compile(r"\s+(rail\s+)?station$", re.IGNORECASE)


def tidy_station_name(name: str) -> str:
    """"Chippenham Station" -> "Chippenham".

    RTT location descriptions sometimes end in "Station". That word ends up in
    the device name and therefore in every entity id (sensor.chippenham_station_
    departures_1), so drop it. Falls back to the original if nothing is left.
    """
    return _STATION_SUFFIX_RE.sub("", name).strip() or name


def parse_dt(value: Any) -> datetime | None:
    """Parse an RTT ISO 8601 string into an aware datetime (None if missing/invalid)."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UK_TZ)
    return parsed


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def planned_time(block: dict[str, Any] | None) -> datetime | None:
    """The advertised (public timetable) time, falling back to the internal WTT time."""
    if not block:
        return None
    return parse_dt(block.get("scheduleAdvertised")) or parse_dt(block.get("scheduleInternal"))


def forecast_time(block: dict[str, Any] | None) -> datetime | None:
    """RTT's forecast, or its estimate when the train has not reported yet."""
    if not block:
        return None
    return parse_dt(block.get("realtimeForecast")) or parse_dt(block.get("realtimeEstimate"))


def actual_time(block: dict[str, Any] | None) -> datetime | None:
    return parse_dt(block.get("realtimeActual")) if block else None


def best_time(block: dict[str, Any] | None) -> datetime | None:
    """Best knowledge of when this will happen: actual > forecast > planned."""
    return actual_time(block) or forecast_time(block) or planned_time(block)


def delay_minutes(block: dict[str, Any] | None) -> int | None:
    """Minutes late (negative = early). 0 if only the timetable is known."""
    planned = planned_time(block)
    if planned is None:
        return None
    expected = actual_time(block) or forecast_time(block)
    if expected is not None:
        return round((expected - planned).total_seconds() / 60)
    lateness = (block or {}).get("realtimeAdvertisedLateness")
    return lateness if isinstance(lateness, int) else 0


def _ts(value: datetime | None) -> float:
    return value.timestamp() if value else float("inf")


def _names(pairs: Any) -> str | None:
    """Join the location names of an origin/destination array ("A / B")."""
    names: list[str] = []
    for pair in pairs or []:
        name = ((pair or {}).get("location") or {}).get("description")
        if name and name not in names:
            names.append(name)
    return " / ".join(names) or None


def _primary_code(location: dict[str, Any]) -> str | None:
    codes = (location.get("shortCodes") or []) + (location.get("longCodes") or [])
    return codes[0] if codes else None


def _code_matches(location: dict[str, Any] | None, code: str) -> bool:
    """Does this GeographicLocation match the home station code (short or long)?"""
    location = location or {}
    wanted = code.upper()
    codes = (location.get("shortCodes") or []) + (location.get("longCodes") or [])
    return any(isinstance(c, str) and c.upper() == wanted for c in codes)


# ---------------------------------------------------------------------------
# Step 1: choose which line-up entries become departures
# ---------------------------------------------------------------------------
def is_boardable_departure(svc: dict[str, Any]) -> bool:
    """True if this line-up entry is a train you could still catch from the home station."""
    t = svc.get("temporalData") or {}
    dep = t.get("departure")
    if not dep:
        return False                      # no departure time here at all
    if t.get("displayAs") in _NON_DEPARTURE_DISPLAY:
        return False                      # passes through, or terminates here
    call_type = t.get("realtimeCallType") or t.get("scheduledCallType")
    if call_type in _NON_BOARDABLE_CALL_TYPES:
        return False                      # set-down only / operational stop
    if dep.get("realtimeActual"):
        return False                      # already left
    return True


def select_departures(location: dict[str, Any] | None, max_count: int) -> list[dict[str, Any]]:
    """Return up to `max_count` upcoming departures, soonest expected first."""
    if not location:
        return []
    candidates = [s for s in (location.get("services") or []) if is_boardable_departure(s)]
    candidates.sort(
        key=lambda s: (
            _ts(best_time((s.get("temporalData") or {}).get("departure"))),
            _ts(planned_time((s.get("temporalData") or {}).get("departure"))),
        )
    )
    return candidates[:max_count]


def departure_time_of(svc: dict[str, Any]) -> datetime | None:
    return best_time((svc.get("temporalData") or {}).get("departure"))


def departure_time_key(svc: dict[str, Any]) -> str:
    """The times a train is due to leave the home station, as one comparable string.

    This is the ONLY thing that decides whether cached service detail is still
    good: the coordinator re-fetches a service only when this key changes
    between two Location lookups (i.e. RTT moved the planned/forecast/estimated/
    actual time at the home station). Everything else on the board (platform,
    status, reasons, ...) comes fresh from the Location lookup every poll.
    """
    dep = (svc.get("temporalData") or {}).get("departure") or {}
    keys = ("scheduleAdvertised", "scheduleInternal", "realtimeForecast", "realtimeEstimate", "realtimeActual")
    return "|".join(str(dep.get(k) or "") for k in keys)


def is_cancelled(svc: dict[str, Any]) -> bool:
    """True if this line-up entry is cancelled (or diverted away)."""
    t = svc.get("temporalData") or {}
    return t.get("displayAs") in ("CANCELLED", "DIVERTED") or bool((t.get("departure") or {}).get("isCancelled"))


# ---------------------------------------------------------------------------
# Step 2: build the sensor data
# ---------------------------------------------------------------------------
def _time_fields(prefix: str, block: dict[str, Any] | None, out: dict[str, Any]) -> None:
    """Add <prefix>_planned/_forecast/_actual to `out` (only for values that exist)."""
    if not block:
        return
    for suffix, value in (
        ("planned", planned_time(block)),
        ("forecast", forecast_time(block)),
        ("actual", actual_time(block)),
    ):
        if value is not None:
            out[f"{prefix}_{suffix}"] = value.isoformat()


def _calling_points(service: dict[str, Any], home_code: str) -> tuple[list[dict[str, Any]], int | None]:
    """Origin -> intermediates -> destination for a service.

    Only places the train calls at are listed (pure passing points are dropped).
    Keys with no value are omitted to keep the attribute small.
    Returns (points, index_of_home_station_or_None).
    """
    points: list[dict[str, Any]] = []
    home_index: int | None = None

    for loc in service.get("locations") or []:
        t = loc.get("temporalData") or {}
        display = t.get("displayAs")
        call_type = t.get("realtimeCallType") or t.get("scheduledCallType")
        if display in (None, "PASS") or call_type == "OPERATIONAL_ONLY":
            continue

        location = loc.get("location") or {}
        arr, dep = t.get("arrival"), t.get("departure")
        plat = (loc.get("locationMetadata") or {}).get("platform") or {}
        main_block = dep or arr or {}

        point: dict[str, Any] = {
            "station": location.get("description"),
            "code": _primary_code(location),
            "call_type": call_type,
            "cancelled": display in ("CANCELLED", "DIVERTED") or bool(main_block.get("isCancelled")),
        }
        _time_fields("arrival", arr, point)
        _time_fields("departure", dep, point)
        point["delay_minutes"] = delay_minutes(main_block)
        point["platform"] = plat.get("actual") or plat.get("forecast") or plat.get("planned")
        point["platform_planned"] = plat.get("planned")

        is_home = _code_matches(location, home_code)
        if is_home and home_index is None:
            home_index = len(points)
            point["is_home"] = True

        points.append({k: v for k, v in point.items() if v is not None})

    # Label first/last so templates don't have to work it out.
    if points:
        points[0]["role"] = "origin"
        points[-1]["role"] = "destination"
        for p in points[1:-1]:
            p["role"] = "intermediate"
    return points, home_index


def _pick_allocation(service: dict[str, Any], allocation_index: Any) -> dict[str, Any]:
    """Choose the allocation (rolling stock) block that applies at the home station."""
    allocations = service.get("allocationData") or []
    for alloc in allocations:
        if allocation_index is not None and alloc.get("allocationIndex") == allocation_index:
            return alloc
    return allocations[0] if allocations else {}


def _reasons(*sources: Any) -> list[dict[str, Any]]:
    """Delay/cancellation reasons from the first source that has any."""
    for source in sources:
        if source:
            return [
                {
                    "type": r.get("type"),
                    "code": r.get("code"),
                    "short_text": r.get("shortText"),
                    # Spec: longText is null when it would equal shortText.
                    "long_text": r.get("longText") or r.get("shortText"),
                }
                for r in source
            ]
    return []


def build_departure(
    line_up: dict[str, Any], service: dict[str, Any] | None, home_code: str
) -> Departure:
    """Combine a line-up entry (always fresh) with cached service detail (may be None)."""
    service = service or {}
    t = line_up.get("temporalData") or {}
    dep = t.get("departure") or {}
    meta = line_up.get("scheduleMetadata") or {}
    lmeta = line_up.get("locationMetadata") or {}
    display = t.get("displayAs")

    planned = planned_time(dep)
    expected = best_time(dep)
    delay = delay_minutes(dep)

    cancelled = bool(dep.get("isCancelled")) or display == "CANCELLED"
    if cancelled:
        status = "cancelled"
    elif display == "DIVERTED":
        status = "diverted"
    elif (delay or 0) >= 1:
        status = "delayed"
    else:
        status = "on_time"

    # Platform: the line-up is refreshed every poll so trust it over the cached detail.
    plat = lmeta.get("platform") or {}
    platform_planned = plat.get("planned")
    platform = plat.get("actual") or plat.get("forecast") or platform_planned

    # Train make-up ("length"). numberOfVehicles is on the line-up; the service
    # allocation (if your token is entitled to it) adds class, unit numbers, facilities.
    alloc = _pick_allocation(service, lmeta.get("allocationIndex"))
    kyt = alloc.get("knowYourTrainData") or {}
    units = [
        i["identity"]
        for i in alloc.get("allocationItems") or []
        if i.get("identity") and not i.get("identitySuppressed")
    ]

    reasons = _reasons(line_up.get("reasons"), service.get("reasons"))

    points, home_index = _calling_points(service, home_code) if service else ([], None)
    calling_at = (
        [p["station"] for p in points[home_index + 1:] if not p.get("cancelled") and p.get("station")]
        if home_index is not None
        else []
    )

    operator = meta.get("operator") or {}
    attributes: dict[str, Any] = {
        # identity
        "service_id": meta.get("uniqueIdentity"),
        "headcode": meta.get("trainReportingIdentity"),
        "operator": operator.get("name"),
        "operator_code": operator.get("code"),
        "mode": meta.get("modeType"),
        # journey
        "origin": _names(line_up.get("origin")) or _names(service.get("origin")),
        "destination": _names(line_up.get("destination")) or _names(service.get("destination")),
        # timing at the home station
        "scheduled_departure": iso(planned),
        "delay_minutes": delay,
        "status": status,
        "cancelled": cancelled,
        "reason": reasons[0]["short_text"] if reasons else None,
        "reasons": reasons,
        "location_status": t.get("status"),   # e.g. AT_PLATFORM, DEPART_READY
        # platform
        "platform": platform,
        "platform_planned": platform_planned,
        "platform_changed": bool(platform and platform_planned and platform != platform_planned),
        # the train itself
        "number_of_vehicles": lmeta.get("numberOfVehicles") or alloc.get("passengerVehicles"),
        "train_class": alloc.get("leadingClass"),
        "units": units,
        "stock_branding": kyt.get("stockBranding") or lmeta.get("stockBranding"),
        "facilities": kyt.get("commonFacilities") or [],
        # route
        "calling_at": calling_at,        # stops after the home station, to the destination
        "calling_points": points,        # full route: origin -> intermediates -> destination
        "detail_available": bool(service),
    }
    return Departure(departure_time=expected, attributes=attributes)
