"""Config and options flows."""
from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bom_flood import analysis as A
from custom_components.bom_flood.config_flow import station_options
from custom_components.bom_flood.const import (
    CONF_CUSTOM_THRESHOLDS,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_STATE,
    CONF_STATION_META,
    CONF_STATIONS,
    DOMAIN,
)

from .fake_bom import FakeBom, popup
from .test_integration import NOW, REGION, entry_data, publish, series

MAP = (
    popup("540001", "Logan River at Waterford", 1.0, "Below Flood Level")
    + popup("540002", "Slacks Creek", 0.4, "No Classification", ("", "", ""))
    + popup("544011", "Warrego River at Augathella", 0.4, "Below Flood Level")
    + popup("544046", "Warrego River at Augathella", 0.4, "No Classification", ("", "", ""))
)


@pytest.fixture
def bom():
    fake = FakeBom()
    fake.maps[REGION] = MAP
    with fake.patch():
        yield fake


def test_station_labels_disambiguate_duplicates():
    stations = A.parse_region_map(MAP)
    labels = {o["value"]: o["label"] for o in station_options(stations)}
    assert labels["544011"] == "Warrego River at Augathella (544011)"
    assert labels["544046"] == "Warrego River at Augathella (544046) · no BOM flood levels"


def test_station_labels_keep_vanished_gauges():
    labels = {o["value"]: o["label"] for o in station_options([], {"999": {"name": "Old Gauge"}})}
    assert labels["999"] == "Old Gauge (999) · no longer on BOM map"


async def test_full_flow(hass, bom):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_STATE: "QLD"})
    assert result["step_id"] == "region"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REGION: REGION})
    assert result["step_id"] == "stations"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_STATIONS: []})
    assert result["errors"] == {CONF_STATIONS: "no_stations"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_STATIONS: ["540001"]})
    assert result["step_id"] == "scan_interval"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.bom_flood.async_setup_entry", _noop_setup)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 15})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "BOM Flood: South East Queensland"
    assert result["data"][CONF_STATION_META]["540001"]["minor_m"] == 2.0
    assert result["result"].unique_id == REGION


async def _noop_setup(hass, entry):
    return True


async def test_duplicate_region_aborts(hass, bom):
    MockConfigEntry(domain=DOMAIN, data=entry_data(), unique_id=REGION).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_STATE: "QLD"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REGION: REGION})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_cannot_connect_and_empty_map(hass, bom):
    bom.fail_prefixes.add("map")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_STATE: "QLD"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REGION: REGION})
    assert result["errors"] == {"base": "cannot_connect"}

    bom.fail_prefixes.clear()
    bom.maps["IDQ65412"] = "<html>BOM changed the page</html>"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_STATE: "QLD"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REGION: "IDQ65412"})
    assert result["errors"] == {"base": "no_gauges_found"}


async def _setup_entry(hass, bom, freezer, data=None):
    freezer.move_to(NOW)
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    bom.maps[REGION] = MAP
    entry = MockConfigEntry(domain=DOMAIN, data=data or entry_data(), unique_id=REGION)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_options_with_vanished_gauge_can_be_saved(hass, bom, freezer):
    """bomflood-tah: a monitored gauge missing from the map must not block saving."""
    data = entry_data()
    data[CONF_STATIONS] = ["540001", "540099"]
    data[CONF_STATION_META]["540099"] = {"station_id": "540099", "name": "Gone Creek", "lat": 0, "lon": 0,
                                         "minor_m": 1.0, "moderate_m": 2.0, "major_m": 3.0}
    entry = await _setup_entry(hass, bom, freezer, data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATIONS: ["540001", "540099"], CONF_SCAN_INTERVAL: 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_SCAN_INTERVAL] == 30
    assert entry.data[CONF_STATION_META]["540099"]["name"] == "Gone Creek"


async def test_options_thresholds_for_unclassified(hass, bom, freezer):
    entry = await _setup_entry(hass, bom, freezer)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATIONS: ["540001", "540002"], CONF_SCAN_INTERVAL: 15}
    )
    assert result["step_id"] == "thresholds"
    fields = {str(k): k for k in result["data_schema"].schema}
    minor = next(k for k in fields if "540002" in k and "Minor" in k)
    moderate = next(k for k in fields if "540002" in k and "Moderate" in k)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {minor: 2.0, moderate: 1.0})
    assert result["errors"] == {"base": "thresholds_order"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {minor: 0.3, moderate: 0.5})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_CUSTOM_THRESHOLDS] == {"540002": {"minor_m": 0.3, "moderate_m": 0.5}}
    assert hass.states.get("sensor.slacks_creek_flood_status").state == "minor"


async def test_deselected_gauge_device_removed(hass, bom, freezer):
    entry = await _setup_entry(hass, bom, freezer)
    registry = dr.async_get(hass)
    assert len(dr.async_entries_for_config_entry(registry, entry.entry_id)) == 2
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATIONS: ["540001"], CONF_SCAN_INTERVAL: 15}
    )
    await hass.async_block_till_done()
    devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    assert [d.name for d in devices] == ["Logan River at Waterford"]
