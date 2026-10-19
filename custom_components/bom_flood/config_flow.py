"""Config and options flows: pick a region, then gauges, then optional levels."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import analysis as A
from .const import (
    BOM_REGIONS,
    BOM_STATES,
    CONF_CUSTOM_THRESHOLDS,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_STATE,
    CONF_STATION_META,
    CONF_STATIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    THRESHOLD_KEYS,
)
from .coordinator import StationMeta, fetch_region_map

_LOGGER = logging.getLogger(__name__)

# Gauge heights run from below zero (tidal, gauge datum) to over 1,100 m on
# AHD-referenced dams, so user levels need a wide range.
THRESHOLD_MIN_M = -20.0
THRESHOLD_MAX_M = 2000.0

_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=5, max=60, step=5, mode=NumberSelectorMode.SLIDER, unit_of_measurement="min")
)


def station_options(
    stations: list[A.MapStation], keep: dict[str, dict[str, Any]] | None = None
) -> list[SelectOptionDict]:
    """Selector options labelled by name and station number.

    BOM maps carry distinct gauges with identical names (two "Warrego River at
    Augathella", one classified and current, one not), so the station number is
    always shown. Gauges already monitored but no longer on the map stay
    selectable, so the options form can still be saved.
    """
    options = []
    for s in sorted(stations, key=lambda s: (s.name, s.station_id)):
        classified = any(v is not None for v in (s.minor_m, s.moderate_m, s.major_m))
        flag = "" if classified else " · no BOM flood levels"
        options.append(SelectOptionDict(value=s.station_id, label=f"{s.name} ({s.station_id}){flag}"))
    on_map = {s.station_id for s in stations}
    for station_id, meta in (keep or {}).items():
        if station_id not in on_map:
            name = (meta or {}).get("name", station_id)
            options.append(
                SelectOptionDict(value=station_id, label=f"{name} ({station_id}) · no longer on BOM map")
            )
    return options


def _meta_dict(m: A.MapStation) -> dict[str, Any]:
    return StationMeta.from_map(m).to_dict()


class BomFloodConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._state = ""
        self._region = ""
        self._stations: list[A.MapStation] = []
        self._selected: list[str] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._state = user_input[CONF_STATE]
            return await self.async_step_region()
        schema = vol.Schema({
            vol.Required(CONF_STATE): SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=k, label=v) for k, v in BOM_STATES.items()],
                    mode=SelectSelectorMode.LIST,
                )
            )
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_region(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._region = user_input[CONF_REGION]
            await self.async_set_unique_id(self._region)
            self._abort_if_unique_id_configured()
            return await self.async_step_stations()
        regions = BOM_REGIONS.get(self._state, {})
        schema = vol.Schema({
            vol.Required(CONF_REGION): SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=code, label=name) for code, name in regions.items()],
                    mode=SelectSelectorMode.LIST,
                )
            )
        })
        return self.async_show_form(step_id="region", data_schema=schema)

    async def async_step_stations(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if not self._stations:
            try:
                self._stations = await self.hass.async_add_executor_job(fetch_region_map, self._region)
            except Exception:  # noqa: BLE001 - shown to the user as cannot_connect
                _LOGGER.exception("Failed to fetch BOM station list")
                errors["base"] = "cannot_connect"
            else:
                if not self._stations:
                    errors["base"] = "no_gauges_found"

        if user_input is not None and not errors:
            if not user_input.get(CONF_STATIONS):
                errors[CONF_STATIONS] = "no_stations"
            else:
                self._selected = list(user_input[CONF_STATIONS])
                return await self.async_step_scan_interval()

        schema = vol.Schema({
            vol.Required(CONF_STATIONS): SelectSelector(
                SelectSelectorConfig(
                    options=station_options(self._stations),
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        })
        return self.async_show_form(step_id="stations", data_schema=schema, errors=errors)

    async def async_step_scan_interval(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            by_id = {s.station_id: s for s in self._stations}
            region_label = BOM_REGIONS.get(self._state, {}).get(self._region, self._region)
            title = f"BOM Flood: {region_label.split(':')[0]}"
            return self.async_create_entry(
                title=title,
                data={
                    CONF_STATE: self._state,
                    CONF_REGION: self._region,
                    CONF_STATIONS: self._selected,
                    CONF_STATION_META: {
                        sid: _meta_dict(by_id[sid]) for sid in self._selected if sid in by_id
                    },
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                },
            )
        schema = vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): _INTERVAL_SELECTOR
        })
        return self.async_show_form(step_id="scan_interval", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BomFloodOptionsFlow:
        return BomFloodOptionsFlow()


class BomFloodOptionsFlow(OptionsFlow):
    """Change gauges, poll interval, and levels for unclassified gauges."""

    def __init__(self) -> None:
        self._stations: list[A.MapStation] = []
        self._pending: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}
        if not self._stations:
            try:
                self._stations = await self.hass.async_add_executor_job(
                    fetch_region_map, entry.data.get(CONF_REGION, "")
                )
            except Exception:  # noqa: BLE001 - shown to the user as cannot_connect
                _LOGGER.exception("Failed to refresh BOM station list")
                errors["base"] = "cannot_connect"

        existing_meta: dict[str, dict[str, Any]] = entry.data.get(CONF_STATION_META, {})
        current = entry.data.get(CONF_STATIONS, [])

        if user_input is not None and not errors:
            selected: list[str] = user_input[CONF_STATIONS]
            if not selected:
                errors[CONF_STATIONS] = "no_stations"
            else:
                by_id = {s.station_id: s for s in self._stations}
                self._pending = {
                    CONF_STATIONS: selected,
                    CONF_STATION_META: {
                        sid: _meta_dict(by_id[sid]) if sid in by_id else existing_meta.get(sid, {"station_id": sid, "name": sid})
                        for sid in selected
                    },
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                }
                return await self.async_step_thresholds()

        keep = {sid: existing_meta.get(sid, {"name": sid}) for sid in current}
        schema = vol.Schema({
            vol.Required(CONF_STATIONS, default=current): SelectSelector(
                SelectSelectorConfig(
                    options=station_options(self._stations, keep),
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): _INTERVAL_SELECTOR,
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    def _unclassified(self) -> list[dict[str, Any]]:
        """Selected gauges BOM publishes no flood levels for at all."""
        return [
            meta
            for sid in self._pending[CONF_STATIONS]
            if (meta := self._pending[CONF_STATION_META].get(sid))
            and all(meta.get(k) is None for k in THRESHOLD_KEYS)
        ]

    @staticmethod
    def _threshold_fields(unclassified: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
        """(form field, station id, threshold key) for each level to collect.

        The field name doubles as the label, since these are generated per gauge
        and cannot have translations.
        """
        fields = []
        for meta in unclassified:
            for key, label in zip(THRESHOLD_KEYS, ("Minor", "Moderate", "Major"), strict=True):
                fields.append((f"{meta.get('name', meta['station_id'])} ({meta['station_id']}): {label} (m)", meta["station_id"], key))
        return fields

    async def async_step_thresholds(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user supply flood levels for gauges BOM does not classify."""
        unclassified = self._unclassified()
        if not unclassified:
            return self._save({})

        errors: dict[str, str] = {}
        entry = self.config_entry
        existing = entry.options.get(CONF_CUSTOM_THRESHOLDS, entry.data.get(CONF_CUSTOM_THRESHOLDS, {}))
        field_map = self._threshold_fields(unclassified)

        if user_input is not None:
            thresholds: dict[str, dict[str, float]] = {}
            for field, sid, key in field_map:
                value = user_input.get(field)
                if value is not None:
                    thresholds.setdefault(sid, {})[key] = float(value)
            for levels in thresholds.values():
                ordered = [levels[k] for k in THRESHOLD_KEYS if k in levels]
                if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
                    errors["base"] = "thresholds_order"
                    break
            if not errors:
                return self._save(thresholds)

        fields: dict[Any, Any] = {}
        for field, sid, key in field_map:
            current = existing.get(sid, {}).get(key)
            fields[vol.Optional(field, description={"suggested_value": current})] = NumberSelector(
                NumberSelectorConfig(
                    min=THRESHOLD_MIN_M, max=THRESHOLD_MAX_M, step=0.01,
                    mode=NumberSelectorMode.BOX, unit_of_measurement="m",
                )
            )
        names = ", ".join(f"{m.get('name', m['station_id'])} ({m['station_id']})" for m in unclassified)
        return self.async_show_form(
            step_id="thresholds",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"gauges": names},
        )

    def _save(self, thresholds: dict[str, dict[str, float]]) -> ConfigFlowResult:
        entry = self.config_entry
        self.hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, **self._pending, CONF_CUSTOM_THRESHOLDS: thresholds},
        )
        return self.async_create_entry(title="", data={})
