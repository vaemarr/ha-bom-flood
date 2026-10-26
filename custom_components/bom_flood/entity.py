"""Shared entity base for BOM Flood gauges."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import BOM_REGIONS, CONF_REGION, CONF_STATE, DOMAIN, warnings_url
from .coordinator import BomFloodCoordinator, StationReading


class BomFloodEntity(CoordinatorEntity[BomFloodCoordinator]):
    """One entity of one gauge. Each gauge is its own device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BomFloodCoordinator,
        entry: ConfigEntry,
        station_id: str,
        key: str,
        unique_suffix: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{station_id}_{unique_suffix or key}"
        meta = coordinator.station_meta(station_id)
        state = entry.data.get(CONF_STATE, "")
        region = entry.data.get(CONF_REGION, "")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{station_id}")},
            name=meta.name if meta else station_id,
            manufacturer="Bureau of Meteorology",
            model=BOM_REGIONS.get(state, {}).get(region, region),
            serial_number=station_id,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=warnings_url(state),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Staleness, rates and projections depend on the clock as well as on
        # new data. Re-evaluate every minute so a gauge that stops reporting is
        # flagged on time even with a long poll interval or a stalled feed. HA
        # only records a state change when something actually changed.
        self.async_on_remove(
            async_track_time_interval(self.hass, self._tick, timedelta(minutes=1))
        )

    @callback
    def _tick(self, _now) -> None:
        if self.reading is not None:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Unavailable only when there is no reading for this gauge at all.

        A gauge whose feed is briefly unreachable keeps its last reading on
        display (marked stale as it ages); a gauge in the feed with one value
        missing reports that value as unknown.
        """
        return super().available and self._station_id in (self.coordinator.data or {})

    @property
    def reading(self) -> StationReading | None:
        return (self.coordinator.data or {}).get(self._station_id)

    @staticmethod
    def now():
        return dt_util.utcnow()
