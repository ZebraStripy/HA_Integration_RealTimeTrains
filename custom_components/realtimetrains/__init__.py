"""The RealTimeTrains integration: UK departure boards from the RTT API."""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RttApiClient
from .const import CONF_TOKEN
from .coordinator import RttConfigEntry, RttCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: RttConfigEntry) -> bool:
    """Set up one home station from a config entry."""
    client = RttApiClient(async_get_clientsession(hass), entry.data[CONF_TOKEN])
    coordinator = RttCoordinator(hass, entry, client)

    # Does the first poll now. If RTT is unreachable HA retries setup later
    # (ConfigEntryNotReady); if the token is rejected it starts the re-auth flow.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RttConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
