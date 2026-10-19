"""End to end: config entry -> coordinator -> entities, against a fake BOM."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bom_flood.const import (
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_STATE,
    CONF_STATION_META,
    CONF_STATIONS,
    DOMAIN,
    HCS_RAINFALL_HOURLY_PREFIX,
    HCS_WATER_LEVEL_DENSE_PREFIX,
    HCS_WATER_LEVEL_PREFIX,
)

from .fake_bom import FakeBom, hcs, popup

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
LOGAN = "540001"
CREEK = "540002"  # no BOM flood levels
REGION = "IDQ65404"


def entry_data() -> dict:
    return {
        CONF_STATE: "QLD",
        CONF_REGION: REGION,
        CONF_STATIONS: [LOGAN, CREEK],
        CONF_STATION_META: {
            LOGAN: {"station_id": LOGAN, "name": "Logan River at Waterford", "lat": -27.7, "lon": 153.1,
                    "minor_m": 2.0, "moderate_m": 3.0, "major_m": 4.0},
            CREEK: {"station_id": CREEK, "name": "Slacks Creek", "lat": -27.6, "lon": 153.1,
                    "minor_m": None, "moderate_m": None, "major_m": None},
        },
        CONF_SCAN_INTERVAL: 15,
    }


def series(end: datetime, hours: float, level0: float, rate: float, step_min: int = 15):
    """Readings every step_min up to `end`, rising at `rate` m/h."""
    n = int(hours * 60 / step_min)
    return [
        (end - timedelta(minutes=step_min * (n - i)), round(level0 + rate * (i * step_min) / 60, 3))
        for i in range(n + 1)
    ]


def publish(bom: FakeBom, now: datetime, logan, creek=None, rain: float | None = 1.5, datum="LGH"):
    creek = creek or [(t, 0.4) for t, _ in logan]
    dense = [("WL", LOGAN, t, v) for t, v in logan] + [("WL", CREEK, t, v) for t, v in creek]
    bom.publish(HCS_WATER_LEVEL_DENSE_PREFIX, hcs(dense, datum))
    bom.publish(
        HCS_WATER_LEVEL_PREFIX,
        hcs([("WL", LOGAN, *logan[-1]), ("WL", CREEK, *creek[-1])], datum),
    )
    rows = [("RN", LOGAN, now - timedelta(minutes=30), rain)] if rain is not None else []
    rows.append(("WL", "999999", now, 0.0))  # keep the file non-empty
    bom.publish(HCS_RAINFALL_HOURLY_PREFIX, hcs(rows))
    bom.maps[REGION] = popup(LOGAN, "Logan River at Waterford", logan[-1][1], "Below Flood Level") + popup(
        CREEK, "Slacks Creek", 0.4, "No Classification", ("", "", "")
    )


@pytest.fixture
def bom():
    fake = FakeBom()
    with fake.patch():
        yield fake


async def setup(hass: HomeAssistant, bom: FakeBom, freezer) -> MockConfigEntry:
    freezer.move_to(NOW)
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(), unique_id=REGION)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def poll(hass: HomeAssistant, entry, freezer, to: datetime) -> None:
    freezer.move_to(to)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def state(hass, entity_id):
    return hass.states.get(entity_id)


L = "sensor.logan_river_at_waterford"
B = "binary_sensor.logan_river_at_waterford"


async def test_entities_and_values(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.5))
    await setup(hass, bom, freezer)

    assert float(state(hass, f"{L}_water_level").state) == 2.0
    assert state(hass, f"{L}_flood_status").state == "minor"
    assert state(hass, f"{L}_flood_status").attributes["severity"] == 2
    assert state(hass, f"{L}_trend").state == "rising"
    assert float(state(hass, f"{L}_rate_of_rise").state) == pytest.approx(0.5)
    # Next level above 2.0 m is moderate at 3.0 m: 2 h at 0.5 m/h.
    ttn = state(hass, f"{L}_time_to_next_flood_level")
    assert float(ttn.state) == pytest.approx(2.0)
    assert ttn.attributes["next_level"] == "moderate"
    assert state(hass, f"{L}_last_reading").state == NOW.isoformat()
    assert state(hass, f"{B}_reading_out_of_date").state == "off"
    assert state(hass, f"{B}_flooding").state == "on"
    assert float(state(hass, f"{L}_rainfall_last_hour").state) == 1.5

    creek = "sensor.slacks_creek"
    assert state(hass, f"{creek}_flood_status").state == "no_classification"
    assert state(hass, f"{creek}_trend").state == "steady"
    assert state(hass, f"{creek}_time_to_next_flood_level").state == STATE_UNKNOWN
    assert state(hass, "binary_sensor.slacks_creek_flooding").state == "off"


async def test_rainfall_disabled_by_default_when_never_reported(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    await setup(hass, bom, freezer)
    registry = er.async_get(hass)
    assert registry.async_get("sensor.slacks_creek_rainfall_last_hour").disabled_by is not None
    assert registry.async_get(f"{L}_rainfall_last_hour").disabled_by is None


async def test_frozen_gauge_drops_rate_and_projection(hass, bom, freezer):
    """bomflood-hea: a gauge that stops reporting must not keep 'rising, flood in 2 h'."""
    publish(bom, NOW, series(NOW, 2, 1.0, 0.5))
    entry = await setup(hass, bom, freezer)
    assert state(hass, f"{L}_trend").state == "rising"

    # BOM keeps serving the same files: no new readings for 24 h.
    await poll(hass, entry, freezer, NOW + timedelta(hours=24))
    assert state(hass, f"{B}_reading_out_of_date").state == "on"
    assert state(hass, f"{L}_trend").state == STATE_UNKNOWN
    assert state(hass, f"{L}_rate_of_rise").state == STATE_UNKNOWN
    assert state(hass, f"{L}_time_to_next_flood_level").state == STATE_UNKNOWN
    # The level itself stays: it is still the last thing BOM published.
    assert float(state(hass, f"{L}_water_level").state) == 2.0


async def test_staleness_updates_without_a_poll(hass, bom, freezer):
    """Stale must flip on time even if the coordinator never runs again."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    await setup(hass, bom, freezer)
    assert state(hass, f"{B}_reading_out_of_date").state == "off"
    later = NOW + timedelta(minutes=70)
    freezer.move_to(later)
    bom.fail_prefixes.add(HCS_WATER_LEVEL_PREFIX)  # any poll that fires keeps old data
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done()
    assert state(hass, f"{B}_reading_out_of_date").state == "on"


async def test_feed_outage_keeps_readings_then_goes_unavailable(hass, bom, freezer):
    """bomflood-1dt: one FTP failure must not blank every entity."""
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    entry = await setup(hass, bom, freezer)
    bom.fail_prefixes.add(HCS_WATER_LEVEL_PREFIX)

    await poll(hass, entry, freezer, NOW + timedelta(minutes=15))
    assert float(state(hass, f"{L}_water_level").state) == 1.0
    assert state(hass, f"{B}_reading_out_of_date").attributes["feed_ok"] is False

    await poll(hass, entry, freezer, NOW + timedelta(hours=7))
    assert state(hass, f"{L}_water_level").state == STATE_UNAVAILABLE
    # Stale stays meaningful exactly when data stops.
    assert state(hass, f"{B}_reading_out_of_date").state == "on"

    bom.fail_prefixes.clear()
    publish(bom, NOW + timedelta(hours=7, minutes=15), series(NOW + timedelta(hours=7, minutes=15), 2, 1.0, 0.0))
    await poll(hass, entry, freezer, NOW + timedelta(hours=7, minutes=15))
    assert float(state(hass, f"{L}_water_level").state) == 1.0
    assert state(hass, f"{B}_reading_out_of_date").state == "off"


async def test_truncated_file_treated_as_outage(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    entry = await setup(hass, bom, freezer)
    t = NOW + timedelta(minutes=15)
    good = series(t, 2, 1.0, 0.0)
    publish(bom, t, good)
    content = bom.files[HCS_WATER_LEVEL_PREFIX]
    bom.files[HCS_WATER_LEVEL_PREFIX] = re.sub(r"Number of Records: \d+", "Number of Records: 99", content)
    await poll(hass, entry, freezer, t)
    attrs = state(hass, f"{B}_reading_out_of_date").attributes
    assert attrs["feed_ok"] is False
    assert state(hass, f"{L}_water_level").state == "1.0"


async def test_repeated_bad_row_never_shown(hass, bom, freezer):
    """bomflood-3ys at integration level: same bad row on 5 polls."""
    logan = series(NOW, 2, 1.71, 0.0)
    publish(bom, NOW, logan)
    entry = await setup(hass, bom, freezer)
    bad_time = NOW + timedelta(minutes=15)
    bom.publish(HCS_WATER_LEVEL_PREFIX, hcs([("WL", LOGAN, bad_time, -26.34), ("WL", CREEK, bad_time, 0.4)]))
    for i in range(1, 6):
        await poll(hass, entry, freezer, NOW + timedelta(minutes=15 * i))
        assert float(state(hass, f"{L}_water_level").state) == 1.71
    assert state(hass, f"{L}_water_level").attributes["rejected_reading"] == -26.34


async def test_datum_change_withholds_status_until_levels_rechecked(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    entry = await setup(hass, bom, freezer)
    t = NOW + timedelta(minutes=15)
    bom.publish(HCS_WATER_LEVEL_PREFIX, hcs([("WL", LOGAN, t, 74.6), ("WL", CREEK, t, 0.4)], datum="AHD"))
    bom.fail_prefixes.add("map")
    await poll(hass, entry, freezer, t)
    assert state(hass, f"{L}_flood_status").state == STATE_UNKNOWN
    bom.fail_prefixes.clear()
    # The map still shows the old-datum level and thresholds: keep withholding.
    await poll(hass, entry, freezer, t + timedelta(minutes=15))
    assert state(hass, f"{L}_flood_status").state == STATE_UNKNOWN
    # Once BOM's map is on the new datum too, classification resumes.
    bom.maps[REGION] = popup(LOGAN, "Logan River at Waterford", 74.6, "Below Flood Level", ("75.50", "76.00", "77.00"))
    await poll(hass, entry, freezer, t + timedelta(minutes=30))
    s = state(hass, f"{L}_flood_status")
    assert s.state == "below_flood_level"
    assert s.attributes["minor_flood_level_m"] == 75.5


async def test_thresholds_refreshed_from_bom_map(hass, bom, freezer):
    """bomflood-3lq: BOM revising a flood level is picked up without reconfiguring."""
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    entry = await setup(hass, bom, freezer)
    assert state(hass, f"{L}_flood_status").state == "below_flood_level"
    bom.maps[REGION] = popup(LOGAN, "Logan River at Waterford", 1.0, "Minor", ("0.90", "3.00", "4.00"))
    await poll(hass, entry, freezer, NOW + timedelta(hours=1, minutes=1))
    s = state(hass, f"{L}_flood_status")
    assert s.state == "minor"
    assert s.attributes["minor_flood_level_m"] == 0.9
    assert s.attributes["bom_status"] == "minor"


async def test_user_thresholds_override_bom(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    freezer.move_to(NOW)
    data = entry_data()
    data["custom_thresholds"] = {CREEK: {"minor_m": 0.3, "moderate_m": 0.5, "major_m": 0.8}}
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=REGION)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    s = state(hass, "sensor.slacks_creek_flood_status")
    assert s.state == "minor" and s.attributes["threshold_source"] == "user"


async def test_history_survives_restart(hass, bom, freezer, hass_storage):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.5))
    entry = await setup(hass, bom, freezer)
    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert len(stored["stations"][LOGAN]["observations"]) >= 8

    # After restart BOM only offers the latest reading: the rate still works.
    bom.fail_prefixes.add(HCS_WATER_LEVEL_DENSE_PREFIX)
    t = NOW + timedelta(minutes=15)
    bom.publish(HCS_WATER_LEVEL_PREFIX, hcs([("WL", LOGAN, t, 2.125), ("WL", CREEK, t, 0.4)]))
    freezer.move_to(t)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert float(state(hass, f"{L}_rate_of_rise").state) == pytest.approx(0.5)


async def test_corrupt_storage_does_not_block_setup(hass, bom, freezer, hass_storage):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(), unique_id=REGION)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
                                                 "data": {"stations": {LOGAN: {"observations": [[None], "x"]}}, "thresholds": 5}}
    freezer.move_to(NOW)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert float(state(hass, f"{L}_water_level").state) == 1.0


async def test_first_refresh_failure_retries_setup(hass, bom, freezer):
    from homeassistant.config_entries import ConfigEntryState

    bom.fail_prefixes.add(HCS_WATER_LEVEL_PREFIX)
    freezer.move_to(NOW)
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(), unique_id=REGION)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_remove_entry_deletes_storage(hass, bom, freezer, hass_storage):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    entry = await setup(hass, bom, freezer)
    await hass.config_entries.async_unload(entry.entry_id)
    assert f"{DOMAIN}.{entry.entry_id}" in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage


async def test_gauge_missing_from_feed_is_unavailable(hass, bom, freezer):
    logan = series(NOW, 2, 1.0, 0.0)
    bom.publish(HCS_WATER_LEVEL_DENSE_PREFIX, hcs([("WL", LOGAN, t, v) for t, v in logan]))
    bom.publish(HCS_WATER_LEVEL_PREFIX, hcs([("WL", LOGAN, *logan[-1])]))
    bom.publish(HCS_RAINFALL_HOURLY_PREFIX, hcs([("WL", "9", NOW, 0.0)]))
    bom.maps[REGION] = popup(LOGAN, "Logan River at Waterford", 1.0, "Below Flood Level")
    await setup(hass, bom, freezer)
    assert float(state(hass, f"{L}_water_level").state) == 1.0
    assert state(hass, "sensor.slacks_creek_water_level").state == STATE_UNAVAILABLE
