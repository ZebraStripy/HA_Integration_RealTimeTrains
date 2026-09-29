"""Config flow: initial setup, re-authentication and reconfiguration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    RttApiClient,
    RttApiError,
    RttAuthError,
    RttBadRequestError,
    RttNotFoundError,
    RttRateLimitError,
)
from .parsing import tidy_station_name
from .const import (
    CONF_FAST_INTERVAL,
    CONF_FAST_WINDOW,
    CONF_MAX_DEPARTURES,
    CONF_MEDIUM_INTERVAL,
    CONF_MEDIUM_WINDOW,
    CONF_SLOW_INTERVAL,
    CONF_STATION,
    CONF_STATION_NAME,
    CONF_TOKEN,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_OPTIONS,
    DOMAIN,
    MAX_INTERVAL,
    MAX_MAX_DEPARTURES,
    MIN_INTERVAL,
    MIN_MAX_DEPARTURES,
)

_LOGGER = logging.getLogger(__name__)

_TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_COUNT_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_MAX_DEPARTURES, max=MAX_MAX_DEPARTURES, step=1, mode=NumberSelectorMode.BOX
    )
)


def _seconds_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_INTERVAL, max=MAX_INTERVAL, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
        )
    )


def _minutes_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=1, max=180, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min")
    )


class RealTimeTrainsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RealTimeTrainsOptionsFlow:
        """The 'Configure' button on the integration: polling schedule."""
        return RealTimeTrainsOptionsFlow()

    async def _async_validate(self, token: str, station: str) -> tuple[str | None, dict[str, str]]:
        """Check the token and station with a real API call.

        Returns (station_display_name, errors). This exercises the whole auth
        chain (long-lived token -> access token) and the Location lookup.
        """
        client = RttApiClient(async_get_clientsession(self.hass), token)
        try:
            data = await client.async_get_location(station)
        except RttAuthError:
            return None, {"base": "invalid_auth"}
        except RttRateLimitError:
            return None, {"base": "rate_limited"}
        except (RttBadRequestError, RttNotFoundError):
            return None, {"base": "invalid_station"}
        except RttApiError:
            return None, {"base": "cannot_connect"}

        # A 204 (no services right now) returns no body, so fall back to the code.
        name = (((data or {}).get("query") or {}).get("location") or {}).get("description")
        return tidy_station_name(name) if name else station, {}

    # -- initial setup --------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            # Accept "gb-nr:CLJ" as well as "CLJ"; codes are case-insensitive.
            station = user_input[CONF_STATION].strip().split(":")[-1].upper()

            # One config entry per station.
            await self.async_set_unique_id(station)
            self._abort_if_unique_id_configured()

            name, errors = await self._async_validate(token, station)
            if not errors:
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_TOKEN: token,
                        CONF_STATION: station,
                        CONF_STATION_NAME: name,
                        CONF_MAX_DEPARTURES: int(user_input[CONF_MAX_DEPARTURES]),
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_TOKEN): _TOKEN_SELECTOR,
                vol.Required(CONF_STATION): str,
                vol.Required(CONF_MAX_DEPARTURES, default=DEFAULT_MAX_DEPARTURES): _COUNT_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    # -- re-authentication (token revoked / invalid) ---------------------
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            _, errors = await self._async_validate(token, entry.data[CONF_STATION])
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): _TOKEN_SELECTOR}),
            errors=errors,
        )

    # -- reconfigure (change token or departure count) --------------------
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            # Leaving the token blank keeps the existing one.
            token = (user_input.get(CONF_TOKEN) or "").strip() or entry.data[CONF_TOKEN]
            _, errors = await self._async_validate(token, entry.data[CONF_STATION])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_TOKEN: token,
                        CONF_MAX_DEPARTURES: int(user_input[CONF_MAX_DEPARTURES]),
                    },
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_TOKEN): _TOKEN_SELECTOR,
                vol.Required(CONF_MAX_DEPARTURES, default=entry.data[CONF_MAX_DEPARTURES]): _COUNT_SELECTOR,
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


class RealTimeTrainsOptionsFlow(OptionsFlowWithReload):
    """Polling schedule. Saving reloads the integration so the new values apply at once."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            values = {key: int(value) for key, value in user_input.items()}
            if values[CONF_MEDIUM_WINDOW] <= values[CONF_FAST_WINDOW]:
                errors["base"] = "window_order"   # the medium window must be the longer one
            else:
                return self.async_create_entry(data=values)

        # Show what was just typed if there was an error, otherwise the saved values.
        current = {**DEFAULT_OPTIONS, **self.config_entry.options, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(CONF_FAST_WINDOW, default=current[CONF_FAST_WINDOW]): _minutes_selector(),
                vol.Required(CONF_FAST_INTERVAL, default=current[CONF_FAST_INTERVAL]): _seconds_selector(),
                vol.Required(CONF_MEDIUM_WINDOW, default=current[CONF_MEDIUM_WINDOW]): _minutes_selector(),
                vol.Required(CONF_MEDIUM_INTERVAL, default=current[CONF_MEDIUM_INTERVAL]): _seconds_selector(),
                vol.Required(CONF_SLOW_INTERVAL, default=current[CONF_SLOW_INTERVAL]): _seconds_selector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
