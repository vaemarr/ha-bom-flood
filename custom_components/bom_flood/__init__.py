"""BOM Flood Monitor: river levels and flood status from the Bureau of Meteorology."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_STATIONS, DOMAIN, PLATFORMS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import BomFloodCoordinator

    coordinator = BomFloodCoordinator(hass, entry)
    # Restore history before the first poll so rates are available at once.
    await coordinator.async_load_history()
    # Raises ConfigEntryNotReady on failure, and HA retries with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    _remove_stale_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this entry's stored gauge history."""
    from homeassistant.helpers.storage import Store

    from .const import STORAGE_VERSION

    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a gauge's device once it is no longer monitored."""
    monitored = {f"{entry.entry_id}_{sid}" for sid in entry.data.get(CONF_STATIONS, [])}
    return not any(
        domain == DOMAIN and ident in monitored for domain, ident in device.identifiers
    )


def _remove_stale_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove devices for gauges deselected in the options."""
    registry = dr.async_get(hass)
    monitored = {f"{entry.entry_id}_{sid}" for sid in entry.data.get(CONF_STATIONS, [])}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not any(domain == DOMAIN and ident in monitored for domain, ident in device.identifiers):
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
