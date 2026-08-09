from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

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
from .coordinator import (
    StationMeta,
    _fetch_html_map,
    _parse_stations_from_html,
)

_LOGGER = logging.getLogger(__name__)


class BomFloodConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._state: str = ""
        self._region: str = ""
        self._stations_meta: list[StationMeta] = []
        self._prefill: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._state = user_input[CONF_STATE]
            return await self.async_step_region()

        schema = vol.Schema({
            vol.Required(CONF_STATE): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=k, label=v)
                        for k, v in BOM_STATES.items()
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            )
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_region(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._region = user_input[CONF_REGION]
            return await self.async_step_stations()

        regions = BOM_REGIONS.get(self._state, {})
        schema = vol.Schema({
            vol.Required(CONF_REGION): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=code, label=name)
                        for code, name in regions.items()
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            )
        })
        return self.async_show_form(step_id="region", data_schema=schema)

    async def async_step_stations(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if not self._stations_meta:
            try:
                html = await self.hass.async_add_executor_job(
                    _fetch_html_map, self._region
                )
                self._stations_meta = _parse_stations_from_html(html)
            except Exception:
                _LOGGER.exception("Failed to fetch BOM station list")
                errors["base"] = "cannot_connect"

        if user_input is not None and not errors:
            selected_ids: list[str] = user_input[CONF_STATIONS]
            if not selected_ids:
                errors[CONF_STATIONS] = "no_stations"
            else:
                meta_by_id = {m.station_id: m for m in self._stations_meta}
                self._prefill = {
                    CONF_STATIONS: selected_ids,
                    CONF_STATION_META: {
                        sid: meta_by_id[sid].to_dict()
                        for sid in selected_ids
                        if sid in meta_by_id
                    },
                }
                return await self.async_step_scan_interval()

        options = [
            SelectOptionDict(value=m.station_id, label=m.name)
            for m in sorted(self._stations_meta, key=lambda m: m.name)
        ]
        schema = vol.Schema({
            vol.Required(CONF_STATIONS): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        })
        return self.async_show_form(
            step_id="stations", data_schema=schema, errors=errors
        )

    async def async_step_scan_interval(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            region_label = BOM_REGIONS.get(self._state, {}).get(self._region, self._region)
            title = f"BOM Flood — {BOM_STATES.get(self._state, self._state)} — {region_label.split('(')[0].strip()}"
            entry_data = {
                CONF_STATE: self._state,
                CONF_REGION: self._region,
                CONF_STATIONS: self._prefill[CONF_STATIONS],
                CONF_STATION_META: self._prefill[CONF_STATION_META],
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
            }
            return self.async_create_entry(title=title, data=entry_data)

        schema = vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
                NumberSelectorConfig(min=5, max=60, step=5, mode=NumberSelectorMode.SLIDER)
            )
        })
        return self.async_show_form(step_id="scan_interval", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BomFloodOptionsFlow:
        return BomFloodOptionsFlow(config_entry)


class BomFloodOptionsFlow(OptionsFlow):

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._stations_meta: list[StationMeta] = []
        self._pending: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        region = self._entry.data.get(CONF_REGION, "")

        if not self._stations_meta:
            try:
                html = await self.hass.async_add_executor_job(_fetch_html_map, region)
                self._stations_meta = _parse_stations_from_html(html)
            except Exception:
                _LOGGER.exception("Failed to refresh BOM station list")
                errors["base"] = "cannot_connect"

        if user_input is not None and not errors:
            selected_ids: list[str] = user_input[CONF_STATIONS]
            meta_by_id = {m.station_id: m for m in self._stations_meta}
            existing_meta = self._entry.data.get(CONF_STATION_META, {})
            station_meta = {
                sid: meta_by_id[sid].to_dict() if sid in meta_by_id else existing_meta.get(sid, {})
                for sid in selected_ids
            }
            self._pending = {
                CONF_STATIONS: selected_ids,
                CONF_STATION_META: station_meta,
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
            }
            return await self.async_step_thresholds()

        current_stations = self._entry.data.get(CONF_STATIONS, [])
        current_interval = self._entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

        options = [
            SelectOptionDict(value=m.station_id, label=m.name)
            for m in sorted(self._stations_meta, key=lambda m: m.name)
        ]
        schema = vol.Schema({
            vol.Required(CONF_STATIONS, default=current_stations): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_SCAN_INTERVAL, default=current_interval): NumberSelector(
                NumberSelectorConfig(min=5, max=60, step=5, mode=NumberSelectorMode.SLIDER)
            ),
        })
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )

    def _unclassified(self) -> list[dict[str, Any]]:
        """Selected gauges BOM publishes no minor flood level for."""
        return [
            meta
            for sid in self._pending[CONF_STATIONS]
            if (meta := self._pending[CONF_STATION_META].get(sid))
            and meta.get("minor_m") is None
        ]

    @staticmethod
    def _threshold_fields(
        unclassified: list[dict[str, Any]],
    ) -> list[tuple[str, str, str]]:
        """(form field, station id, threshold key) for each level to collect.

        The field name doubles as the label HA renders, since these are generated
        per gauge and so cannot be given translations in strings.json.
        """
        fields = []
        for meta in unclassified:
            for key, label in zip(THRESHOLD_KEYS, ("Minor", "Moderate", "Major")):
                fields.append(
                    (f"{meta['name']} — {label} (m)", meta["station_id"], key)
                )
        return fields

    async def async_step_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user supply flood levels for gauges BOM does not classify.

        BOM leaves these empty for ALERT-type gauges, which makes a flood status
        impossible to derive from the feed alone. Skipped entirely when every
        selected gauge already has BOM thresholds.
        """
        unclassified = self._unclassified()
        if not unclassified:
            return self._save({})

        errors: dict[str, str] = {}
        existing = self._entry.options.get(
            CONF_CUSTOM_THRESHOLDS, self._entry.data.get(CONF_CUSTOM_THRESHOLDS, {})
        )

        field_map = self._threshold_fields(unclassified)

        if user_input is not None:
            thresholds: dict[str, dict[str, float]] = {}
            for field, sid, key in field_map:
                value = user_input.get(field)
                if value is not None:
                    thresholds.setdefault(sid, {})[key] = float(value)
            for sid, levels in thresholds.items():
                ordered = [levels[k] for k in THRESHOLD_KEYS if k in levels]
                if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
                    errors["base"] = "thresholds_order"
                    break
            if not errors:
                return self._save(thresholds)

        fields: dict[Any, Any] = {}
        for field, sid, key in field_map:
            current = existing.get(sid, {}).get(key)
            fields[
                vol.Optional(field, description={"suggested_value": current})
            ] = NumberSelector(
                NumberSelectorConfig(min=0, max=50, step=0.01, mode=NumberSelectorMode.BOX)
            )

        names = ", ".join(m["name"] for m in unclassified)
        return self.async_show_form(
            step_id="thresholds",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"gauges": names},
        )

    def _save(self, thresholds: dict[str, dict[str, float]]) -> FlowResult:
        new_data = {
            **self._entry.data,
            **self._pending,
            CONF_CUSTOM_THRESHOLDS: thresholds,
        }
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        return self.async_create_entry(title="", data={})
