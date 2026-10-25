"""Binary sensors for each BOM river gauge."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import FLOOD_STATUS_MAJOR, FLOOD_STATUS_MINOR, FLOOD_STATUS_MODERATE
from .coordinator import BomFloodCoordinator, StationReading
from .entity import BomFloodEntity

PARALLEL_UPDATES = 0

_FLOODING = {FLOOD_STATUS_MINOR, FLOOD_STATUS_MODERATE, FLOOD_STATUS_MAJOR}


@dataclass(frozen=True, kw_only=True)
class BomBinaryDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[StationReading, datetime], bool | None]
    attrs_fn: Callable[[StationReading, datetime], dict[str, Any]] | None = None


def _stale_attrs(r: StationReading, now: datetime) -> dict[str, Any]:
    return {
        "stale_after_min": r.stale_after_min,
        "feed_ok": r.feed_ok,
    }


def _warning_attrs(r: StationReading, now: datetime) -> dict[str, Any]:
    w, f = r.warning, r.forecast
    return {
        "title": w.title if w else None,
        "issued": w.issued if w else None,
        "final": w.final if w else None,
        "url": w.url if w else None,
        "forecast_level_m": f.level_m if f else None,
        "forecast_class": f.flood_class if f else None,
        "forecast_when": f.when if f else None,
        "forecast_direction": f.direction if f else None,
        "bom_says": " ".join(f.sentences)[:600] if f else None,
        "warnings_checked": r.warnings_ok,
    }


BINARY_SENSORS: tuple[BomBinaryDescription, ...] = (
    BomBinaryDescription(
        key="stale",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda r, now: r.is_stale(now),
        attrs_fn=_stale_attrs,
    ),
    BomBinaryDescription(
        key="bom_warning",
        device_class=BinarySensorDeviceClass.SAFETY,
        # A "Final" warning is BOM's all clear, so it does not turn this on.
        is_on_fn=lambda r, now: (
            (r.warning is not None and not r.warning.final) if r.warnings_ok is not None else None
        ),
        attrs_fn=_warning_attrs,
    ),
    BomBinaryDescription(
        key="flooding",
        device_class=BinarySensorDeviceClass.MOISTURE,
        is_on_fn=lambda r, now: (r.flood_status in _FLOODING) if r.flood_status else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BomFloodCoordinator = entry.runtime_data
    async_add_entities(
        BomFloodBinarySensor(coordinator, entry, station_id, description)
        for station_id in coordinator.station_ids
        for description in BINARY_SENSORS
    )


class BomFloodBinarySensor(BomFloodEntity, BinarySensorEntity):
    entity_description: BomBinaryDescription

    def __init__(
        self,
        coordinator: BomFloodCoordinator,
        entry: ConfigEntry,
        station_id: str,
        description: BomBinaryDescription,
    ) -> None:
        super().__init__(coordinator, entry, station_id, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        # "Stale" must stay meaningful exactly when the data stops arriving.
        if self.entity_description.key == "stale":
            return self.coordinator.last_update_success or bool(self.coordinator.data)
        return super().available

    @property
    def is_on(self) -> bool | None:
        r = self.reading
        if r is None:
            return True if self.entity_description.key == "stale" else None
        return self.entity_description.is_on_fn(r, self.now())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        r = self.reading
        if r is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(r, self.now())
