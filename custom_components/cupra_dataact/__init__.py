"""CUPRA vehicle data for Home Assistant via the VW Group EU Data Act portal."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .api import DataActClient
from .const import CONF_BRAND, CONF_SCAN_INTERVAL, DEFAULT_BRAND, DEFAULT_SCAN_INTERVAL
from .coordinator import DataActCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

type DataActConfigEntry = ConfigEntry[DataActCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DataActConfigEntry) -> bool:
    client = DataActClient(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_BRAND, DEFAULT_BRAND),
    )
    minutes = entry.options.get(CONF_SCAN_INTERVAL)
    interval = timedelta(minutes=minutes) if minutes else DEFAULT_SCAN_INTERVAL
    coordinator = DataActCoordinator(hass, entry, client, interval)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.close()
        raise

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DataActConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: DataActConfigEntry) -> None:
    from homeassistant.helpers.storage import Store

    from .const import DOMAIN, STORAGE_VERSION

    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _async_reload(hass: HomeAssistant, entry: DataActConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
