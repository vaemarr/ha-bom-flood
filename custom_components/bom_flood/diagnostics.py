"""Diagnostics download: what the integration holds for each gauge."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    now = dt_util.utcnow()
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "coordinator": coordinator.diagnostics(now),
    }
