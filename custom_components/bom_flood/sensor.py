from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPrecipitationDepth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_FLOOD_STATUS,
    ATTR_LAST_OBSERVED,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_MAJOR_LEVEL,
    ATTR_MINOR_LEVEL,
    ATTR_MODERATE_LEVEL,
    ATTR_QUALITY,
    ATTR_RATE_OF_RISE,
    ATTR_STATION_ID,
    ATTR_TIME_TO_MAJOR,
    ATTR_TIME_TO_MINOR,
    ATTR_TIME_TO_MODERATE,
    ATTR_TREND,
    BOM_REGIONS,
    CONF_REGION,
    CONF_STATE,
    CONF_STATION_META,
    CONF_STATIONS,
    DOMAIN,
    FLOOD_STATUS_BELOW,
    FLOOD_STATUS_MAJOR,
    FLOOD_STATUS_MINOR,
    FLOOD_STATUS_MODERATE,
    FLOOD_STATUS_UNKNOWN,
)
from .coordinator import BomFloodCoordinator, StationMeta

_LOGGER = logging.getLogger(__name__)


def _hours_to_threshold(
    level: float | None,
    rate: float | None,
    threshold: float | None,
) -> float | None:
    """Hours until threshold at current rate of rise. None if falling, steady, or already past threshold."""
    if level is None or rate is None or threshold is None or rate <= 0 or level >= threshold:
        return None
    return round((threshold - level) / rate, 1)


def _station_device_info(entry: ConfigEntry, station_id: str, name: str) -> DeviceInfo:
    state = entry.data.get(CONF_STATE, "")
    region = entry.data.get(CONF_REGION, "")
    model = BOM_REGIONS.get(state, {}).get(region, region)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{station_id}")},
        name=name,
        manufacturer="Bureau of Meteorology",
        model=model,
        configuration_url="https://www.bom.gov.au/waterdata/",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BomFloodCoordinator = hass.data[DOMAIN][entry.entry_id]
    station_ids: list[str] = entry.data[CONF_STATIONS]
    station_meta: dict[str, dict] = entry.data.get(CONF_STATION_META, {})

    entities: list[SensorEntity] = []
    for station_id in station_ids:
        meta_dict = station_meta.get(station_id, {})
        entities.append(BomFloodLevelSensor(coordinator, entry, station_id, meta_dict))
        entities.append(BomFloodStatusSensor(coordinator, entry, station_id, meta_dict))
        entities.append(BomFloodRateOfRiseSensor(coordinator, entry, station_id, meta_dict))
        entities.append(BomFloodRainfallSensor(coordinator, entry, station_id, meta_dict))

    async_add_entities(entities)


class _BomFloodBaseSensor(CoordinatorEntity[BomFloodCoordinator], SensorEntity):
    """Shared base for all BOM flood sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BomFloodCoordinator,
        entry: ConfigEntry,
        station_id: str,
        meta_dict: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._meta = StationMeta.from_dict(meta_dict) if meta_dict else None
        self._entry = entry
        self._station_name = meta_dict.get("name", station_id)

    @property
    def device_info(self) -> DeviceInfo:
        return _station_device_info(self._entry, self._station_id, self._station_name)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self._station_id in (self.coordinator.data or {})
        )

    def _reading(self):
        return (self.coordinator.data or {}).get(self._station_id)


class BomFloodLevelSensor(_BomFloodBaseSensor):
    """Water level in metres. Primary sensor for each gauge."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "m"
    _attr_icon = "mdi:waves"

    def __init__(self, coordinator, entry, station_id, meta_dict):
        super().__init__(coordinator, entry, station_id, meta_dict)
        self._attr_unique_id = f"{entry.entry_id}_{station_id}_level"
        self._attr_name = "Water Level"

    @property
    def native_value(self) -> float | None:
        r = self._reading()
        return r.level if r else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self._reading()
        attrs: dict[str, Any] = {ATTR_STATION_ID: self._station_id}
        if r:
            attrs[ATTR_FLOOD_STATUS] = r.flood_status
            attrs[ATTR_TREND] = r.trend
            attrs[ATTR_LAST_OBSERVED] = r.timestamp
            attrs[ATTR_QUALITY] = r.quality
        if self._meta:
            attrs[ATTR_MINOR_LEVEL] = self._meta.minor_m
            attrs[ATTR_MODERATE_LEVEL] = self._meta.moderate_m
            attrs[ATTR_MAJOR_LEVEL] = self._meta.major_m
            attrs[ATTR_LATITUDE] = self._meta.lat
            attrs[ATTR_LONGITUDE] = self._meta.lon
            if r and r.rate_of_rise_m_per_hr is not None:
                attrs[ATTR_TIME_TO_MINOR] = _hours_to_threshold(
                    r.level, r.rate_of_rise_m_per_hr, self._meta.minor_m
                )
                attrs[ATTR_TIME_TO_MODERATE] = _hours_to_threshold(
                    r.level, r.rate_of_rise_m_per_hr, self._meta.moderate_m
                )
                attrs[ATTR_TIME_TO_MAJOR] = _hours_to_threshold(
                    r.level, r.rate_of_rise_m_per_hr, self._meta.major_m
                )
        return attrs


class BomFloodStatusSensor(_BomFloodBaseSensor):
    """Flood status as a readable state: Below Flood Level / Minor / Moderate / Major."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        FLOOD_STATUS_BELOW,
        FLOOD_STATUS_MINOR,
        FLOOD_STATUS_MODERATE,
        FLOOD_STATUS_MAJOR,
        FLOOD_STATUS_UNKNOWN,
    ]
    _attr_icon = "mdi:water-alert"

    def __init__(self, coordinator, entry, station_id, meta_dict):
        super().__init__(coordinator, entry, station_id, meta_dict)
        self._attr_unique_id = f"{entry.entry_id}_{station_id}_flood_status"
        self._attr_name = "Flood Status"

    @property
    def native_value(self) -> str | None:
        r = self._reading()
        return r.flood_status if r else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self._reading()
        if not r:
            return {}
        return {
            ATTR_TREND: r.trend,
            ATTR_LAST_OBSERVED: r.timestamp,
            ATTR_MINOR_LEVEL: self._meta.minor_m if self._meta else None,
            ATTR_MODERATE_LEVEL: self._meta.moderate_m if self._meta else None,
            ATTR_MAJOR_LEVEL: self._meta.major_m if self._meta else None,
        }


class BomFloodRateOfRiseSensor(_BomFloodBaseSensor):
    """Rate of water level change in m/hr. Positive = rising, negative = falling."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "m/h"
    _attr_icon = "mdi:arrow-up-down"
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, entry, station_id, meta_dict):
        super().__init__(coordinator, entry, station_id, meta_dict)
        self._attr_unique_id = f"{entry.entry_id}_{station_id}_rate_of_rise"
        self._attr_name = "Rate of Rise"

    @property
    def native_value(self) -> float | None:
        r = self._reading()
        return r.rate_of_rise_m_per_hr if r else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self._reading()
        return {ATTR_TREND: r.trend} if r else {}


class BomFloodRainfallSensor(_BomFloodBaseSensor):
    """Hourly rainfall in mm. Only available when BOM provides rainfall data for this station."""

    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, coordinator, entry, station_id, meta_dict):
        super().__init__(coordinator, entry, station_id, meta_dict)
        self._attr_unique_id = f"{entry.entry_id}_{station_id}_rainfall_1h"
        self._attr_name = "Hourly Rainfall"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        r = self._reading()
        return r is not None and r.rainfall_mm_1h is not None

    @property
    def native_value(self) -> float | None:
        r = self._reading()
        return r.rainfall_mm_1h if r else None
