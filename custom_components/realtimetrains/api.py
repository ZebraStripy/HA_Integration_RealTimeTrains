"""Thin async client for the Realtime Trains (RTT) API, version 2.

Responsibilities of this module (and nothing else):
  * Authentication: exchange the long-lived token for a short-life access token,
    keep it in memory, and renew it when it is missing (start-up) or expired.
  * Make the three calls we need: get_access_token, location, service.
  * Read the X-RateLimit-* headers on every response so the rest of the
    integration knows how many calls are left.
  * Translate HTTP failures into a small set of exceptions.

It knows nothing about Home Assistant entities - see coordinator.py for that.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from homeassistant.util import dt as dt_util

from .const import (
    API_BASE_URL,
    API_VERSION,
    ENDPOINT_ACCESS_TOKEN,
    ENDPOINT_LOCATION,
    ENDPOINT_SERVICE,
    RATE_LIMIT_DIMENSIONS,
    REQUEST_TIMEOUT,
    STATUS_UNKNOWN,
    TOKEN_EXPIRY_MARGIN,
)

_LOGGER = logging.getLogger(__name__)

# If the token endpoint doesn't tell us when the token expires, assume this.
_FALLBACK_TOKEN_LIFETIME_SECONDS = 600
# If a 429 doesn't carry a Retry-After header, wait this long.
_DEFAULT_RETRY_AFTER = 60


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class RttApiError(Exception):
    """Base class for all RTT API problems."""


class RttAuthError(RttApiError):
    """The long-lived token (or access token) was rejected."""


class RttRateLimitError(RttApiError):
    """HTTP 429. `retry_after` is the number of seconds RTT asked us to wait."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Rate limit exceeded; retry after {retry_after}s")
        self.retry_after = retry_after


class RttConnectionError(RttApiError):
    """Network problem or timeout talking to RTT."""


class RttBadRequestError(RttApiError):
    """HTTP 400 - typically an unknown station code or bad parameter."""


class RttNotFoundError(RttApiError):
    """HTTP 404 - e.g. a service that has dropped out of the system."""


# ---------------------------------------------------------------------------
# Status object shared with the API status sensor
# ---------------------------------------------------------------------------
@dataclass
class RttApiStatus:
    """Everything we know about how the API is behaving right now."""

    state: str = STATUS_UNKNOWN            # ok / degraded / rate_limited / auth_error / error
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    last_error: str | None = None
    calls_made: int = 0                    # HTTP calls since Home Assistant started
    limits: dict[str, int] = field(default_factory=dict)      # {"Minute": 60, ...}
    remaining: dict[str, int] = field(default_factory=dict)   # {"Minute": 57, ...}
    system_status: dict[str, str] = field(default_factory=dict)  # RTT's own health report
    entitlements: list[str] = field(default_factory=list)
    token_valid_until: datetime | None = None

    def lowest_fraction(self, dimensions: tuple[str, ...] | None = None) -> float | None:
        """Smallest remaining/limit across the given dimensions (None if unknown)."""
        fractions = [
            remaining / self.limits[dim]
            for dim, remaining in self.remaining.items()
            if (dimensions is None or dim in dimensions) and self.limits.get(dim)
        ]
        return min(fractions) if fractions else None

    def is_low(self) -> bool:
        """True when any quota is nearly exhausted (<10% or <=1 call left)."""
        for dim, remaining in self.remaining.items():
            if remaining <= 1:
                return True
            limit = self.limits.get(dim)
            if limit and remaining / limit < 0.10:
                return True
        return False


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class RttApiClient:
    """Async RTT API client with automatic access-token management."""

    def __init__(self, session: aiohttp.ClientSession, long_lived_token: str) -> None:
        self._session = session
        self._long_lived_token = long_lived_token
        # Access token lives in memory only. It is nil at start-up and is
        # fetched lazily on the first request.
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._token_lock = asyncio.Lock()
        self.status = RttApiStatus()

    # -- public API ---------------------------------------------------------
    async def async_get_location(
        self, code: str, time_window_minutes: int | None = None
    ) -> dict[str, Any] | None:
        """Departures line-up for a station. Returns None if nothing is running (HTTP 204)."""
        params: dict[str, str] = {"code": code}
        if time_window_minutes:
            params["timeWindow"] = str(time_window_minutes)
        return await self._get(ENDPOINT_LOCATION, params)

    async def async_get_service(self, identity: str, departure_date: str) -> dict[str, Any] | None:
        """Full detail (all calling points, allocation) for one service.

        Returns the inner ``service`` object, or None if RTT returned nothing.
        We use identity + departureDate rather than uniqueIdentity because the
        gb-nr endpoint wants the identity *without* the "gb-nr:" prefix, and
        sending the two parts separately avoids any ambiguity.
        """
        data = await self._get(
            ENDPOINT_SERVICE, {"identity": identity, "departureDate": departure_date}
        )
        return (data or {}).get("service")

    # -- authentication -----------------------------------------------------
    async def _async_ensure_access_token(self) -> None:
        """Get a new access token if we have none (start-up) or it has expired."""
        if self._token_is_valid():
            return

        async with self._token_lock:
            # Another task may have refreshed while we waited for the lock.
            if self._token_is_valid():
                return

            _LOGGER.debug("Requesting a new RTT access token")
            # The long-lived token is the Bearer for this one call only.
            data = await self._async_request(
                ENDPOINT_ACCESS_TOKEN, None, self._long_lived_token
            )
            token = (data or {}).get("token")
            if not token:
                raise RttApiError("RTT did not return an access token")

            self._access_token = token

            # `validUntil` is the token's expiry time (ISO 8601).
            expiry = dt_util.parse_datetime((data or {}).get("validUntil") or "")
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry is None:
                expiry = dt_util.utcnow().replace(microsecond=0) + timedelta(
                    seconds=_FALLBACK_TOKEN_LIFETIME_SECONDS
                )
            self._token_expiry = expiry

            self.status.token_valid_until = expiry
            self.status.entitlements = list((data or {}).get("entitlements") or [])

    def _token_is_valid(self) -> bool:
        return bool(
            self._access_token
            and self._token_expiry
            and dt_util.utcnow() < self._token_expiry - TOKEN_EXPIRY_MARGIN
        )

    # -- request plumbing ---------------------------------------------------
    async def _get(self, path: str, params: dict[str, str] | None) -> dict[str, Any] | None:
        """GET with an access token; renew the token and retry once if it is rejected."""
        await self._async_ensure_access_token()
        try:
            return await self._async_request(path, params, self._access_token)
        except RttAuthError:
            # The access token was refused (revoked, or expired earlier than
            # validUntil said). Throw it away, get another, and try once more.
            _LOGGER.debug("Access token rejected; renewing and retrying once")
            self._access_token = None
            await self._async_ensure_access_token()
            return await self._async_request(path, params, self._access_token)

    async def _async_request(
        self, path: str, params: dict[str, str] | None, bearer: str | None
    ) -> dict[str, Any] | None:
        """One HTTP GET. Records rate-limit headers and maps errors to exceptions."""
        headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
        if API_VERSION:
            headers["Version"] = API_VERSION

        self.status.calls_made += 1
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.get(
                    f"{API_BASE_URL}{path}", params=params, headers=headers
                ) as resp:
                    # Rate-limit headers come back on *every* response, including errors.
                    self._record_rate_limits(resp.headers)

                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict) and isinstance(data.get("systemStatus"), dict):
                            self.status.system_status = data["systemStatus"]
                        return data
                    if resp.status == 204:
                        return None  # valid query, nothing to report
                    if resp.status in (401, 403):
                        raise RttAuthError(f"RTT rejected the token (HTTP {resp.status})")
                    if resp.status == 400:
                        raise RttBadRequestError("RTT rejected the request (HTTP 400) - check the station code")
                    if resp.status == 404:
                        raise RttNotFoundError("Not found (HTTP 404)")
                    if resp.status == 429:
                        retry_after = _parse_int(resp.headers.get("Retry-After")) or _DEFAULT_RETRY_AFTER
                        raise RttRateLimitError(retry_after)
                    raise RttApiError(f"Unexpected HTTP {resp.status} from RTT")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RttConnectionError(f"Error talking to RTT: {err}") from err

    def _record_rate_limits(self, headers: Any) -> None:
        """Store X-RateLimit-Limit-<dim> / X-RateLimit-Remaining-<dim> for each dimension."""
        for dim in RATE_LIMIT_DIMENSIONS:
            limit = _parse_int(headers.get(f"X-RateLimit-Limit-{dim}"))
            remaining = _parse_int(headers.get(f"X-RateLimit-Remaining-{dim}"))
            if limit is not None:
                self.status.limits[dim] = limit
            if remaining is not None:
                self.status.remaining[dim] = remaining


def _parse_int(value: str | None) -> int | None:
    """int() that returns None for missing/garbage values."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
