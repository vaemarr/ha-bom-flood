"""Sensors for each BOM river gauge."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    FLOOD_SEVERITY,
    FLOOD_STATUS_OPTIONS,
    TREND_OPTIONS,
)
from .coordinator import BomFloodCoordinator, StationReading
from .entity import BomFloodEntity

# Entities only read coordinator data, so there is nothing to serialise.
PARALLEL_UPDATES = 0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(frozen=True, kw_only=True)
class BomSensorDescription(SensorEntityDescription):
    value_fn: Callable[[StationReading, datetime], Any]
    attrs_fn: Callable[[StationReading, datetime], dict[str, Any]] | None = None
    unique_suffix: str | None = None


def _level_attrs(r: StationReading, now: datetime) -> dict[str, Any]:
    return {
        "station_id": r.station_id,
        "observed_at": _iso(r.observed_at),
        "datum": r.datum,
        "minor_flood_level_m": r.thresholds.get("minor"),
        "moderate_flood_level_m": r.thresholds.get("moderate"),
        "major_flood_level_m": r.thresholds.get("major"),
        "rejected_reading": r.rejected_reading,
        # A big jump waiting for the next reading before it is shown.
        "unconfirmed_reading": r.unconfirmed_level,
        "unconfirmed_at": _iso(r.unconfirmed_at),
        # Tidal gauge: rises and falls within tide_range_m are the tide.
        "tidal": r.tidal,
        "tide_range_m": r.tide_range_m,
        "level_shift_at": _iso(r.level_shift_at),
        "quality": r.quality,
        "latitude": r.lat,
        "longitude": r.lon,
    }


def _status_attrs(r: StationReading, now: datetime) -> dict[str, Any]:
    return {
        "severity": FLOOD_SEVERITY.get(r.flood_status) if r.flood_status else None,
        "stale": r.is_stale(now),
        "minor_flood_level_m": r.thresholds.get("minor"),
        "moderate_flood_level_m": r.thresholds.get("moderate"),
        "major_flood_level_m": r.thresholds.get("major"),
        "threshold_source": r.thresholds_source,
        "thresholds_checked_at": _iso(r.thresholds_checked_at),
        # BOM's own displayed classification, from its regional map, so any
        # disagreement with the value computed here is visible.
        "bom_status": r.bom_status,
    }


def _rate_attrs(r: StationReading, now: datetime) -> dict[str, Any]:
    return {
        "window_hours": r.rate_window_h if r.rate(now) is not None else None,
        "observed_at": _iso(r.observed_at),
    }


def _next_level_attrs(r: StationReading, now: datetime) -> dict[str, Any]:
    level, _ = r.next_level(now)
    hours = r.hours_to(now)
    earliest = r.earliest_to(now)
    return {
        "next_level": level,
        # State: the earliest likely time (reached no earlier than this in ~94%
        # of past floods). Straight-line times at the current rate follow.
        "at_current_rate_h": hours[level] if level else None,
        "earliest_minor_h": earliest["minor"],
        "earliest_moderate_h": earliest["moderate"],
        "earliest_major_h": earliest["major"],
        "time_to_minor_h": hours["minor"],
        "time_to_moderate_h": hours["moderate"],
        "time_to_major_h": hours["major"],
        "rate_m_per_h": r.rate(now),
        "heads_up": r.heads_up_h(now) is not None,
        "method": "earliest likely time, calibrated on past floods; not a BOM forecast",
    }


def _rain_attrs(r: StationReading, now: datetime) -> dict[str, Any]:
    return {
        "observed_at": _iso(r.rainfall_observed_at),
        "last_15_min_mm": r.rainfall_15min_mm,
        "source": r.rainfall_source,
    }


SENSORS: tuple[BomSensorDescription, ...] = (
    BomSensorDescription(
        key="water_level",
        unique_suffix="level",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda r, now: r.level,
        attrs_fn=_level_attrs,
    ),
    BomSensorDescription(
        key="flood_status",
        device_class=SensorDeviceClass.ENUM,
        options=FLOOD_STATUS_OPTIONS,
        value_fn=lambda r, now: r.flood_status,
        attrs_fn=_status_attrs,
    ),
    BomSensorDescription(
        key="rate_of_rise",
        native_unit_of_measurement="m/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda r, now: r.rate(now),
        attrs_fn=_rate_attrs,
    ),
    BomSensorDescription(
        key="trend",
        device_class=SensorDeviceClass.ENUM,
        options=TREND_OPTIONS,
        value_fn=lambda r, now: r.trend(now),
    ),
    BomSensorDescription(
        key="time_to_next_level",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value_fn=lambda r, now: r.next_level(now)[1],
        attrs_fn=_next_level_attrs,
    ),
    BomSensorDescription(
        key="last_observed",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda r, now: r.observed_at,
    ),
    BomSensorDescription(
        key="rainfall",
        unique_suffix="rainfall_1h",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r, now: r.rainfall_mm,
        attrs_fn=_rain_attrs,
    ),
    BomSensorDescription(
        key="stale_after",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda r, now: r.stale_after_min,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BomFloodCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for station_id in coordinator.station_ids:
        for description in SENSORS:
            entities.append(BomFloodSensor(coordinator, entry, station_id, description))
    async_add_entities(entities)


class BomFloodSensor(BomFloodEntity, SensorEntity):
    entity_description: BomSensorDescription

    def __init__(
        self,
        coordinator: BomFloodCoordinator,
        entry: ConfigEntry,
        station_id: str,
        description: BomSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, station_id, description.key, description.unique_suffix)
        self.entity_description = description
        if description.key == "rainfall" and not coordinator.reports_rainfall(station_id):
            # Most gauges never report rain; an always-unknown sensor is noise.
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> Any:
        r = self.reading
        return self.entity_description.value_fn(r, self.now()) if r else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        r = self.reading
        if r is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(r, self.now())
