"""DataUpdateCoordinator: decides WHEN to call the API and WHAT to call.

One refresh cycle:
  1. Location lookup (1 API call)  -> the list of upcoming departures.
  2. Service lookups (0..3 calls)  -> full journey for trains we have never looked
     up, or whose due time at the home station has changed since we last looked.
     Otherwise the cached copy is reused.
  3. Build one `Departure` per sensor (padded with None if fewer trains exist).

After each cycle we choose how long to wait before the next one:
  * the SCHEDULE (how soon the next train leaves), but never sooner than
  * the BUDGET GOVERNOR allows (keeps us inside the RTT quota) - see budget.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import parsing
from .api import (
    RttApiClient,
    RttApiError,
    RttAuthError,
    RttRateLimitError,
)
from .budget import CallBudget
from .const import (
    CONF_BUDGET_SHARE,
    CONF_FAST_INTERVAL,
    CONF_FAST_WINDOW,
    CONF_MAX_DEPARTURES,
    CONF_MEDIUM_INTERVAL,
    CONF_MEDIUM_WINDOW,
    CONF_SLOW_INTERVAL,
    CONF_STATION,
    CONF_STATION_NAME,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOCATION_TIME_WINDOW_MINUTES,
    MAX_SERVICE_CALLS_PER_UPDATE,
    STATUS_AUTH_ERROR,
    STATUS_DEGRADED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_RATE_LIMITED,
)
from .parsing import Departure

_LOGGER = logging.getLogger(__name__)

type RttConfigEntry = ConfigEntry[RttCoordinator]


@dataclass
class RttData:
    """What the coordinator publishes to the entities."""

    # Always exactly `max_departures` long; None = "no train for this slot".
    departures: list[Departure | None]
    # Changes on every refresh, which guarantees entities (notably the status
    # sensor, whose rate-limit counters move) are told about every update.
    fetched_at: datetime
    calls_in_update: int


@dataclass
class _CachedService:
    """A service-detail response, and the home-station due time it was fetched for."""

    time_key: str          # parsing.departure_time_key() at the moment we fetched
    service: dict[str, Any]


class RttCoordinator(DataUpdateCoordinator[RttData]):
    """Polls RTT for the home station."""

    config_entry: RttConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RttConfigEntry, client: RttApiClient) -> None:
        self.client = client
        self.station_code: str = entry.data[CONF_STATION]
        self.station_name: str = parsing.tidy_station_name(
            entry.data.get(CONF_STATION_NAME) or self.station_code
        )
        self.max_departures: int = int(entry.data[CONF_MAX_DEPARTURES])

        # Polling schedule from the integration's options (defaults if never set).
        opts = {**DEFAULT_OPTIONS, **entry.options}
        self.fast_interval = int(opts[CONF_FAST_INTERVAL])
        self.medium_interval = int(opts[CONF_MEDIUM_INTERVAL])
        self.slow_interval = int(opts[CONF_SLOW_INTERVAL])
        self.fast_window = int(opts[CONF_FAST_WINDOW])        # minutes
        self.medium_window = int(opts[CONF_MEDIUM_WINDOW])    # minutes

        # Quota governor: average usage capped at this share of the RTT limits.
        self.budget = CallBudget(share=int(opts[CONF_BUDGET_SHARE]) / 100)
        self.budget_delay: float = 0.0   # seconds the governor added to the last interval

        # uniqueIdentity -> cached service detail
        self._service_cache: dict[str, _CachedService] = {}
        self._next_due: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.station_code}",
            config_entry=entry,
            update_interval=timedelta(seconds=self.fast_interval),
        )

    # ------------------------------------------------------------------
    async def _async_update_data(self) -> RttData:
        status = self.client.status
        status.last_attempt = dt_util.utcnow()
        calls_before = status.calls_made

        try:
            data = await self._async_poll(calls_before)
        except (UpdateFailed, ConfigEntryAuthFailed):
            # Failed polls still cost calls. Count them, and make sure repeated
            # failures can't hammer the API faster than the budget allows.
            self._record_calls(calls_before)
            self.update_interval = timedelta(
                seconds=max(self.update_interval.total_seconds(), self._budget_wait())
            )
            raise

        self._record_calls(calls_before)
        self.update_interval = self._choose_interval()
        return data

    async def _async_poll(self, calls_before: int) -> RttData:
        """One refresh cycle (see module docstring). Raises UpdateFailed on API problems."""
        status = self.client.status
        try:
            # 1. One call gets the whole departure board for the window.
            location = await self.client.async_get_location(
                self.station_code, LOCATION_TIME_WINDOW_MINUTES
            )  # None means HTTP 204: nothing running - not an error.
            candidates = parsing.select_departures(location, self.max_departures)

            # 2. Enrich departures with journey detail (cached where possible).
            details, degraded = await self._async_get_details(candidates)

        except RttAuthError as err:
            # Bad/revoked long-lived token: ask the user to re-enter it.
            self._record_failure(STATUS_AUTH_ERROR, err)
            raise ConfigEntryAuthFailed(str(err)) from err
        except RttRateLimitError as err:
            # Obey Retry-After: don't poll again until RTT says we may.
            self._record_failure(STATUS_RATE_LIMITED, err)
            self.update_interval = timedelta(seconds=max(err.retry_after + 1, self.fast_interval))
            raise UpdateFailed(str(err)) from err
        except RttApiError as err:
            self._record_failure(STATUS_ERROR, err)
            raise UpdateFailed(str(err)) from err

        # Remember when the next catchable train leaves; _choose_interval() uses it.
        self._next_due = next(
            (parsing.departure_time_of(s) for s in candidates if not parsing.is_cancelled(s)), None
        )

        # 3. One Departure per requested sensor; unused slots stay None.
        departures: list[Departure | None] = [
            parsing.build_departure(
                svc,
                details.get((svc.get("scheduleMetadata") or {}).get("uniqueIdentity")),
                self.station_code,
            )
            for svc in candidates
        ]
        departures += [None] * (self.max_departures - len(departures))

        status.state = STATUS_DEGRADED if degraded else STATUS_OK
        status.last_success = dt_util.utcnow()
        status.last_error = None

        return RttData(
            departures=departures,
            fetched_at=dt_util.utcnow(),
            calls_in_update=status.calls_made - calls_before,
        )

    # ------------------------------------------------------------------
    async def _async_get_details(
        self, candidates: list[dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Return ({uniqueIdentity: service}, degraded).

        Cache rule: a service lookup is made only if we have no cached copy of
        that train, or the Location lookup shows its due time at the home
        station has changed since the cached copy was fetched. There is no
        time-based expiry.

        At most MAX_SERVICE_CALLS_PER_UPDATE lookups are made per refresh (nearest
        trains first); the rest wait for the next refresh. This keeps us inside
        RTT's per-minute limit, e.g. when filling the cache after a restart.

        `degraded` is True if any detail lookup was skipped or failed. The
        departure is still shown - just without (fresh) calling points.
        """
        details: dict[str, dict[str, Any]] = {}
        degraded = False
        stop_fetching = False   # set when we hit a rate limit mid-way
        fetched = 0

        for svc in candidates:
            meta = svc.get("scheduleMetadata") or {}
            key = meta.get("uniqueIdentity")
            identity = meta.get("identity")
            date = meta.get("departureDate")
            if not (key and identity and date):
                degraded = True  # can't look this one up
                continue

            time_key = parsing.departure_time_key(svc)
            cached = self._service_cache.get(key)

            # Cache hit: the due time at the home station has not changed -> no API call.
            if cached and cached.time_key == time_key:
                details[key] = cached.service
                continue

            # Hold back: per-update cap reached, or a quota is nearly gone. Keep
            # any (stale) cached copy and save calls for the location lookup.
            if stop_fetching or fetched >= MAX_SERVICE_CALLS_PER_UPDATE or self.client.status.is_low():
                if cached:
                    details[key] = cached.service
                degraded = True
                continue

            try:
                fetched += 1
                service = await self.client.async_get_service(identity, date)
            except RttAuthError:
                raise
            except RttRateLimitError:
                stop_fetching = True
                degraded = True
                if cached:
                    details[key] = cached.service
                continue
            except RttApiError as err:
                # One bad service must not break the whole board.
                _LOGGER.debug("Service lookup failed for %s: %s", key, err)
                degraded = True
                if cached:
                    details[key] = cached.service
                continue

            if service:
                self._service_cache[key] = _CachedService(time_key, service)
                details[key] = service
            else:
                degraded = True

        # Forget trains that are no longer on the board so the cache stays small.
        wanted = {(s.get("scheduleMetadata") or {}).get("uniqueIdentity") for s in candidates}
        for key in list(self._service_cache):
            if key not in wanted:
                del self._service_cache[key]

        return details, degraded

    # ------------------------------------------------------------------
    def _record_calls(self, calls_before: int) -> None:
        """Tell the governor how many API calls the update just made."""
        status = self.client.status
        self.budget.record(dt_util.utcnow(), status.calls_made - calls_before, status.limits, status.remaining)

    def _budget_wait(self) -> float:
        """Seconds the governor needs before the next poll is affordable."""
        return self.budget.seconds_until_allowed(self.client.status.limits)

    def _choose_interval(self) -> timedelta:
        """How long to wait before the next poll.

        1. Schedule: driven by how soon the next (non-cancelled) train leaves.
        2. Governor: never sooner than the API quota budget allows.
        """
        if self._next_due is None:
            seconds = self.slow_interval                       # nothing due at all
        else:
            minutes_to_go = (self._next_due - dt_util.utcnow()).total_seconds() / 60
            if minutes_to_go < self.fast_window:
                seconds = self.fast_interval                   # due soon (or already late)
            elif minutes_to_go < self.medium_window:
                seconds = self.medium_interval
            else:
                seconds = self.slow_interval                   # nothing due for a while

        wait = self._budget_wait()
        self.budget_delay = max(0.0, wait - seconds)
        return timedelta(seconds=max(seconds, wait))

    def _record_failure(self, state: str, err: Exception) -> None:
        self.client.status.state = state
        self.client.status.last_error = str(err)
