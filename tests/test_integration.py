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
    HCS_RAINFALL_15MIN_PREFIX,
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
    # Next level above 2.0 m is moderate at 3.0 m: 2 h at 0.5 m/h in a
    # straight line; the state is the calibrated earliest likely time.
    from custom_components.bom_flood.analysis import earliest_hours

    ttn = state(hass, f"{L}_time_to_next_flood_level")
    assert ttn.attributes["at_current_rate_h"] == pytest.approx(2.0)
    assert float(ttn.state) == earliest_hours(2.0) < 2.0
    assert ttn.attributes["next_level"] == "moderate"
    assert ttn.attributes["time_to_moderate_h"] == pytest.approx(2.0)
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


async def test_diagnostics(hass, bom, freezer):
    from custom_components.bom_flood.diagnostics import async_get_config_entry_diagnostics

    publish(bom, NOW, series(NOW, 2, 1.0, 0.5))
    entry = await setup(hass, bom, freezer)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    gauge = diag["coordinator"]["gauges"][LOGAN]
    assert gauge["rate"]["rate_m_per_h"] == pytest.approx(0.5)
    assert gauge["meta"]["minor_m"] == 2.0
    assert len(gauge["history"]) >= 8


async def test_unexpected_unit_is_ignored(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    entry = await setup(hass, bom, freezer)
    t = NOW + timedelta(minutes=15)
    content = hcs([("WL", LOGAN, t, 150.0), ("WL", CREEK, t, 0.4)]).replace('150.0,"metres"', '150.0,"centimetres"')
    bom.publish(HCS_WATER_LEVEL_PREFIX, content)
    await poll(hass, entry, freezer, t)
    assert float(state(hass, f"{L}_water_level").state) == 1.0


async def test_bom_flood_warning_for_gauge(hass, bom, freezer):
    import pathlib

    text = (pathlib.Path(__file__).parent / "fixtures" / "IDQ20885.txt").read_text()
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    entry = await setup(hass, bom, freezer)
    assert state(hass, f"{B}_bom_flood_warning").state == "off"

    bom.warnings["IDQ20885"] = text
    await poll(hass, entry, freezer, NOW + timedelta(minutes=15))
    s = state(hass, f"{B}_bom_flood_warning")
    assert s.state == "on"
    assert s.attributes["title"] == "Major Flood Warning for the Logan and Albert Rivers"
    assert s.attributes["forecast_level_m"] == 13.5
    assert s.attributes["forecast_when"] == "Monday afternoon"
    assert state(hass, "binary_sensor.slacks_creek_bom_flood_warning").state == "off"

    # A failed fetch keeps the last known warning instead of clearing it.
    bom.fail_prefixes.add("warnings")
    await poll(hass, entry, freezer, NOW + timedelta(minutes=30))
    s = state(hass, f"{B}_bom_flood_warning")
    assert s.state == "on" and s.attributes["warnings_checked"] is False

    bom.fail_prefixes.clear()
    bom.warnings.clear()
    await poll(hass, entry, freezer, NOW + timedelta(minutes=45))
    assert state(hass, f"{B}_bom_flood_warning").state == "off"


def quarters(end: datetime, values: list[float]) -> str:
    """15-minute rain for Logan, the last value ending at `end`."""
    n = len(values)
    rows = [("RN15", LOGAN, end - timedelta(minutes=15 * (n - 1 - i)), v) for i, v in enumerate(values)]
    return hcs(rows + [("WL", "999999", end, 0.0)])


async def test_rain_uses_rolling_15_minute_hour(hass, bom, freezer):
    """Hourly says 1.5 mm to 09:30; 15-minute readings to 09:45 give a fresher hour."""
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    bom.publish(HCS_RAINFALL_15MIN_PREFIX, quarters(NOW - timedelta(minutes=15), [0.5, 1.0, 2.0, 4.0, 0.5]))
    entry = await setup(hass, bom, freezer)
    s = state(hass, f"{L}_rainfall_last_hour")
    assert float(s.state) == 7.5  # 1.0 + 2.0 + 4.0 + 0.5
    assert s.attributes["source"] == "15-minute"
    assert s.attributes["last_15_min_mm"] == 0.5
    assert s.attributes["observed_at"].startswith("2026-10-19T09:45")

    # Next quarter arrives: the hour moves on without waiting for the hourly file.
    bom.publish(HCS_RAINFALL_15MIN_PREFIX, quarters(NOW, [0.5, 1.0, 2.0, 4.0, 0.5, 3.0]))
    await poll(hass, entry, freezer, NOW + timedelta(minutes=5))
    assert float(state(hass, f"{L}_rainfall_last_hour").state) == 9.5


async def test_rain_gap_in_15_minute_data_falls_back_to_hourly(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    bom.publish(HCS_RAINFALL_15MIN_PREFIX, hcs([
        ("RN15", LOGAN, NOW - timedelta(minutes=45), 1.0),
        ("RN15", LOGAN, NOW - timedelta(minutes=15), 1.0),  # 09:30 missing
        ("RN15", LOGAN, NOW, 1.0),
    ]))
    await setup(hass, bom, freezer)
    s = state(hass, f"{L}_rainfall_last_hour")
    assert float(s.state) == 1.5 and s.attributes["source"] == "hourly"
    assert s.attributes["last_15_min_mm"] is None


async def test_rain_files_only_downloaded_when_new(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    bom.publish(HCS_RAINFALL_15MIN_PREFIX, quarters(NOW, [0.0] * 4))
    entry = await setup(hass, bom, freezer)
    await poll(hass, entry, freezer, NOW + timedelta(minutes=5))
    rain_downloads = [n for n in bom.fetched if n.startswith(("IDZ65900", "IDZ65901"))]
    assert len(rain_downloads) == 2  # one each, not again on the second poll
    s = state(hass, f"{L}_rainfall_last_hour")
    assert float(s.state) == 0.0 and s.attributes["source"] == "15-minute"


async def test_final_warning_is_all_clear(hass, bom, freezer):
    import pathlib

    text = (pathlib.Path(__file__).parent / "fixtures" / "IDQ20885.txt").read_text()
    final = text.replace("Major Flood Warning for the Logan", "Final Flood Warning for the Logan")
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    bom.warnings["IDQ20885"] = final
    await setup(hass, bom, freezer)
    s = state(hass, f"{B}_bom_flood_warning")
    assert s.state == "off"
    assert s.attributes["final"] is True and s.attributes["title"].startswith("Final")


async def test_heads_up_needs_two_qualifying_readings(hass, bom, freezer):
    """Minor 2.0 m. Rising 0.3 m/h: on once two readings are both >= 1.0 m and <= 4 h out."""
    logan = series(NOW, 2, 0.4, 0.3)  # ends at 1.0 m; the reading before is 0.925 m
    publish(bom, NOW, logan)
    entry = await setup(hass, bom, freezer)
    assert state(hass, f"{B}_flood_heads_up").state == "off"

    later = NOW + timedelta(minutes=15)
    publish(bom, later, logan + [(later, 1.075)])
    await poll(hass, entry, freezer, later)
    s = state(hass, f"{B}_flood_heads_up")
    assert s.state == "on"
    assert s.attributes["hours_to_minor"] == pytest.approx((2.0 - 1.075) / 0.3, abs=0.1)
    assert state(hass, f"{L}_time_to_next_flood_level").attributes["heads_up"] is True
    # No flood levels: unknown, not "no heads-up".
    assert state(hass, "binary_sensor.slacks_creek_flood_heads_up").state == STATE_UNKNOWN


async def test_heads_up_ignores_rise_far_below_minor(hass, bom, freezer):
    """Logan Village tides: 0.5 m/h towards a 2 m minor from 0.6 m is ~3 h, but only 30% of minor."""
    publish(bom, NOW, series(NOW, 2, -0.4, 0.5))  # ends at 0.6 m
    await setup(hass, bom, freezer)
    assert state(hass, f"{L}_time_to_next_flood_level").attributes["time_to_minor_h"] <= 4
    assert state(hass, f"{B}_flood_heads_up").state == "off"


async def test_spike_held_until_next_reading(hass, bom, freezer):
    """Wolffdene, Oct 2026: 1.80 -> 2.65 -> 1.80 m must never show 2.65 m."""
    steady = series(NOW, 2, 1.8, 0.0)
    publish(bom, NOW, steady)
    entry = await setup(hass, bom, freezer)

    t1 = NOW + timedelta(minutes=15)
    publish(bom, t1, steady + [(t1, 2.65)])
    await poll(hass, entry, freezer, t1)
    level = state(hass, f"{L}_water_level")
    assert float(level.state) == 1.8
    assert level.attributes["unconfirmed_reading"] == 2.65

    t2 = t1 + timedelta(minutes=15)
    publish(bom, t2, steady + [(t1, 2.65), (t2, 1.8)])
    await poll(hass, entry, freezer, t2)
    level = state(hass, f"{L}_water_level")
    assert float(level.state) == 1.8
    assert level.attributes["unconfirmed_reading"] is None
    assert level.attributes["observed_at"] == t2.isoformat()


async def test_stale_limit_learns_publication_delay(hass, bom, freezer):
    """Canungra: readings every 5 min, published hourly at :45 up to :00. Must stop flapping."""
    def upto(end):
        return series(end, 3, 1.0, 0.0, step_min=5)

    publish(bom, NOW, upto(NOW - timedelta(minutes=45)))
    entry = await setup(hass, bom, freezer)
    assert state(hass, f"{B}_reading_out_of_date").attributes["stale_after_min"] == 60

    now, flags = NOW, []
    for _ in range(5 * 4):  # five hours of 15-minute polls
        now += timedelta(minutes=15)
        if now.minute == 45:
            publish(bom, now, upto(now.replace(minute=0)))
        await poll(hass, entry, freezer, now)
        flags.append((now, state(hass, f"{B}_reading_out_of_date").state))
    # Usual age before the next batch is 1 h 45 min, so the limit becomes 1.5x that.
    assert state(hass, f"{B}_reading_out_of_date").attributes["stale_after_min"] == pytest.approx(157.5)
    # Once three publications have been seen, it never flips to out of date.
    assert all(f == "off" for t, f in flags if t >= NOW + timedelta(hours=2, minutes=45))
    assert any(f == "on" for _, f in flags)  # it did flap before learning


WATCH = """IDQ20800
Australian Government Bureau of Meteorology
Queensland

Flood Watch for South East Queensland Coastal Catchments

Issued at 4:05 pm EST on Friday 23 October 2026

Flood Watch Number: 1

Heavy rainfall may cause minor to moderate flooding from Saturday.

Catchments likely to be affected include:
Noosa River
Pine and Caboolture Rivers
Logan and Albert Rivers
Coomera River

Next issue:
The next Flood Watch will be issued by 4:00 pm Saturday.
"""


async def test_warning_for_gauges_river_when_gauge_not_named(hass, bom, freezer):
    """Most gauges are not BOM forecast points: match by the river in the title."""
    import pathlib

    text = (pathlib.Path(__file__).parent / "fixtures" / "IDQ20885.txt").read_text()
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    bom.warnings["IDQ20885"] = text.replace("Waterford", "Elsewhere")
    entry = await setup(hass, bom, freezer)
    s = state(hass, f"{B}_bom_flood_warning")
    assert s.state == "on"
    assert s.attributes["match"] == "river"
    assert s.attributes["forecast_level_m"] is None
    assert state(hass, "binary_sensor.slacks_creek_bom_flood_warning").state == "off"

    # The same river name in another state's warning is a different river.
    bom.warnings.clear()
    bom.warnings["IDN20885"] = text.replace("Waterford", "Elsewhere")
    await poll(hass, entry, freezer, NOW + timedelta(minutes=15))
    assert state(hass, f"{B}_bom_flood_warning").state == "off"


async def test_flood_watch_is_an_attribute_not_a_warning(hass, bom, freezer):
    publish(bom, NOW, series(NOW, 2, 1.0, 0.0))
    bom.warnings["IDQ20800"] = WATCH
    await setup(hass, bom, freezer)
    s = state(hass, f"{B}_bom_flood_warning")
    assert s.state == "off"
    assert s.attributes["watch_title"] == "Flood Watch for South East Queensland Coastal Catchments"
    assert s.attributes["watch_issued"].startswith("4:05 pm")
    assert state(hass, "binary_sensor.slacks_creek_bom_flood_warning").attributes["watch_title"] is None


async def test_tidal_gauge_rise_within_tide_is_not_a_flood_signal(hass, bom, freezer):
    """Logan Village: the tide coming in must not say rising/flood soon/heads-up."""
    import math

    def tide(t: datetime) -> float:
        h = (t - NOW).total_seconds() / 3600
        return round(0.6 + 0.35 * math.sin(2 * math.pi * h / 12.42), 3)

    def upto(end):
        return [(t, tide(t)) for t, _ in series(end, 39.75, 0, 0)]

    publish(bom, NOW, upto(NOW))
    entry = await setup(hass, bom, freezer)
    now = NOW
    for _ in range(26):
        now += timedelta(hours=1)
        publish(bom, now, upto(now))
        await poll(hass, entry, freezer, now)
    assert state(hass, f"{L}_water_level").attributes["tidal"] is True

    # Advance to the rising half of the tide.
    while not (tide(now) - tide(now - timedelta(minutes=15)) > 0.02 and tide(now) > 0.7):
        now += timedelta(minutes=15)
        publish(bom, now, upto(now))
        await poll(hass, entry, freezer, now)
    assert state(hass, f"{L}_trend").state == "tidal"
    assert state(hass, f"{L}_time_to_next_flood_level").state == STATE_UNKNOWN
    assert state(hass, f"{B}_flood_heads_up").state == "off"

    # The river rises above every recent high tide: back to normal behaviour.
    rising = upto(now)
    for i in range(1, 6):
        rising.append((now + timedelta(minutes=15 * i), round(tide(now) + 0.12 * i, 3)))
    now = rising[-1][0]
    publish(bom, now, rising)
    await poll(hass, entry, freezer, now)
    assert state(hass, f"{L}_trend").state == "rising"
    assert state(hass, f"{L}_time_to_next_flood_level").state != STATE_UNKNOWN


async def test_tide_handling_stops_at_half_of_minor(hass, bom, freezer):
    """Even a gauge flagged tidal shows a normal trend once at half of minor or above."""
    publish(bom, NOW, series(NOW, 2, 0.6, 0.4))  # rising to 1.4 m; minor 2.0
    entry = await setup(hass, bom, freezer)
    tracker = entry.runtime_data._trackers[LOGAN]
    tracker.tidal = True
    await poll(hass, entry, freezer, NOW + timedelta(minutes=1))
    assert state(hass, f"{L}_trend").state == "rising"
    assert state(hass, f"{L}_time_to_next_flood_level").state != STATE_UNKNOWN
