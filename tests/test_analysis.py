"""Pure logic: parsing, classification, screening, rates, projections.

Many tests reproduce failures found in the October 2026 review (bead ids in
the docstrings) so they cannot come back.
"""
from __future__ import annotations

import math
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.bom_flood import analysis as A
from custom_components.bom_flood.const import (
    FLOOD_STATUS_BELOW,
    FLOOD_STATUS_MAJOR,
    FLOOD_STATUS_MINOR,
    FLOOD_STATUS_MODERATE,
    FLOOD_STATUS_NO_CLASSIFICATION,
)

T0 = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def obs(minutes: float, level: float) -> A.Observation:
    return A.Observation(at(minutes), level)


def feed(tracker: A.GaugeTracker, series: list[tuple[float, float]]) -> list[A.IngestResult]:
    return [tracker.ingest(obs(m, v)) for m, v in series]


def steady(start: float, end: float, step: float, level: float) -> list[tuple[float, float]]:
    out, m = [], start
    while m <= end:
        out.append((m, level))
        m += step
    return out


def rising(start: float, end: float, step: float, level0: float, rate_m_per_h: float):
    out, m = [], start
    while m <= end:
        out.append((m, round(level0 + rate_m_per_h * (m - start) / 60, 4)))
        m += step
    return out


# ---------------------------------------------------------------------------
# HCS parsing
# ---------------------------------------------------------------------------

HCS = """# HEADER: Agency Id: BoM
# HEADER: File Generation Date: 2026-10-19T09:08:15Z
# HEADER: Number of Records: 6
# HEADER: Data Fields: IndexNo, SensorType, SensorDataType, SiteIdType, SiteId, ObservationTimestamp, RealValue, Unit, SensorParam1, SensorParam2, Quality, Comment
1,"WL",1,"SR","540001","2026-10-19T08:45:00Z",1.25,"metres","LGH",,1,""
2,"WL",1,"SR","540001","2026-10-19T08:30:00Z",1.20,"metres","LGH",,1,""
3,"WL",1,"SR","540002","2026-10-19T08:40:00Z",-0.4,"metres","AHD",,1,""
4,"RN",1,"SR","540001","2026-10-19T08:00:00Z",3.5,"mm","",3600,3,""
5,"WL",1,"SR","540003","2026-10-19T08:40:00Z",,"metres","LGH",,1,""
6,"WL",1,"SR","540004","2026-10-19T08:40:00",1.0,"metres","LGH",,1,""
"""


def test_parse_hcs_picks_newest_record_not_last_row():
    parsed = A.parse_hcs(HCS, "WL")
    latest = parsed.latest("540001")
    assert latest.value == 1.25
    assert latest.observed_at == datetime(2026, 10, 19, 8, 45, tzinfo=UTC)
    assert len(parsed.records["540001"]) == 2


def test_parse_hcs_keeps_negative_values_datum_and_missing_as_none():
    parsed = A.parse_hcs(HCS, "WL")
    assert parsed.latest("540002").value == -0.4
    assert parsed.latest("540002").datum == "AHD"
    assert parsed.latest("540003").value is None


def test_parse_hcs_rejects_naive_timestamps():
    assert "540004" not in A.parse_hcs(HCS, "WL").records


def test_parse_hcs_rainfall_period():
    rec = A.parse_hcs(HCS, "RN").latest("540001")
    assert rec.value == 3.5 and rec.period_s == 3600 and rec.quality == 3


def test_parse_hcs_detects_truncation():
    """bomflood-3fu: a cut-off download must not parse as valid."""
    truncated = "\n".join(HCS.splitlines()[:7])
    with pytest.raises(A.HcsFormatError):
        A.parse_hcs(truncated, "WL")


def test_parse_hcs_empty_file():
    with pytest.raises(A.HcsFormatError):
        A.parse_hcs("# HEADER: Number of Records: 0\n", "WL")


# ---------------------------------------------------------------------------
# Map parsing
# ---------------------------------------------------------------------------

MAP = (
    "PopupRiver(&quot;Brunswick River at Mullumbimby&quot;,'558006','-28.5528','153.4944','0.09',"
    "'Below Flood Level','falling','19-10-2026 19:30:00','2.50','3.50','4.50','Click')"
    "PopupRiver(&quot;Brunswick River at Durrumbul (Sherry's Bridge)&quot;,'558007','-28.53','153.45','0.90',"
    "'No Classification','steady','19-10-2026 19:27:41','','','','Click')"
    "PopupRiver(&quot;Hawkesbury River at Webbs Ck&quot;,'561004','-33.4','150.9','0.23',"
    "'Below Flood Level','steady','19-10-2026 19:27:41','','3.50','4.20','Click')"
)


def test_parse_region_map():
    stations = {s.station_id: s for s in A.parse_region_map(MAP)}
    s = stations["558006"]
    assert (s.minor_m, s.moderate_m, s.major_m) == (2.5, 3.5, 4.5)
    assert s.bom_status == FLOOD_STATUS_BELOW and s.bom_trend == "falling" and s.bom_level == 0.09
    assert stations["558007"].name == "Brunswick River at Durrumbul (Sherry's Bridge)"
    assert stations["558007"].minor_m is None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "thresholds", "expected"),
    [
        (1.0, (2.0, 3.0, 4.0), FLOOD_STATUS_BELOW),
        (2.0, (2.0, 3.0, 4.0), FLOOD_STATUS_MINOR),
        (3.5, (2.0, 3.0, 4.0), FLOOD_STATUS_MODERATE),
        (9.0, (2.0, 3.0, 4.0), FLOOD_STATUS_MAJOR),
        (-5.0, (2.0, 3.0, 4.0), FLOOD_STATUS_BELOW),
        (1.0, (None, None, None), FLOOD_STATUS_NO_CLASSIFICATION),
        # bomflood-ag8: moderate/major but no minor is Below, as BOM shows it.
        (0.23, (None, 3.5, 4.2), FLOOD_STATUS_BELOW),
        (3.6, (None, 3.5, 4.2), FLOOD_STATUS_MODERATE),
        (1.0, (None, None, 3.8), FLOOD_STATUS_BELOW),
        (8.0, (8.0, 8.0, 9.0), FLOOD_STATUS_MODERATE),
    ],
)
def test_derive_flood_status(level, thresholds, expected):
    assert A.derive_flood_status(level, *thresholds) == expected


def test_no_reading_is_unknown():
    assert A.derive_flood_status(None, 1, 2, 3) is None


# ---------------------------------------------------------------------------
# Trend and projections
# ---------------------------------------------------------------------------


def test_unknown_rate_is_unknown_trend_not_steady():
    """bomflood-kh3."""
    assert A.trend_from_rate(None) is None
    assert A.trend_from_rate(0.0) == "steady"
    assert A.trend_from_rate(0.2) == "rising"
    assert A.trend_from_rate(-0.2) == "falling"
    assert A.trend_from_rate(0.011) == "rising"
    assert A.trend_from_rate(-0.015) == "steady"  # falls need a larger change


def test_no_projection_for_negligible_rate():
    """bomflood-z9v: 0.002 m/h must not project a flood 2094 h away."""
    assert A.hours_to_level(0.81, 0.002, 5.0) is None
    assert A.hours_to_level(0.81, 0.008, 5.0) is None


def test_projection_horizon():
    assert A.hours_to_level(1.0, 0.1, 5.0) == 40.0
    assert A.hours_to_level(1.0, 0.05, 5.0) is None  # 80 h > 48 h horizon


def test_no_projection_once_past_threshold_or_falling():
    assert A.hours_to_level(5.0, 0.5, 5.0) is None
    assert A.hours_to_level(1.0, -0.5, 5.0) is None


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------


def test_genuine_flash_flood_is_accepted():
    """Rises of 1 and 3 m/h, and 2 m in 15 minutes, are real and must pass."""
    for rate in (1.0, 3.0, 8.0):
        tracker = A.GaugeTracker()
        series = rising(75, 180, 15, 1.0 + rate / 4, rate)
        results = feed(tracker, steady(0, 60, 15, 1.0) + series)
        assert not any(r.rejected_level for r in results), rate
        # At most the first jump waits for one more reading to confirm it.
        assert sum(r.held for r in results) <= 1, rate
        assert tracker.latest.level == series[-1][1]
        assert len(tracker.history) == 5 + len(series)


def test_single_dropout_rejected():
    """README example: 1.71 steady, one 0.81, then back to 1.71."""
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 1.71))
    result = tracker.ingest(obs(75, 0.81))
    assert not result.accepted and result.rejected_level == 0.81
    assert tracker.latest.level == 1.71
    assert tracker.ingest(obs(90, 1.70)).accepted


def test_same_bad_row_repeated_never_accepted():
    """bomflood-3ys: one bad BOM row re-read on every poll is one observation."""
    tracker = A.GaugeTracker()
    feed(tracker, [(0, 1.71), (15, 1.72), (30, 1.73)])
    for _ in range(10):
        result = tracker.ingest(obs(45, -26.34))
        assert not result.accepted
    assert tracker.latest.level == 1.73
    assert tracker.ingest(obs(60, 1.74)).accepted


def test_scattered_bad_values_never_add_up_to_a_shift():
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 0.91))
    for i, bad in enumerate((-26.34, 15.0, -9.0, 30.0)):
        assert not tracker.ingest(obs(75 + 15 * i, bad)).accepted
    assert tracker.latest.level == 0.91


def test_consistent_level_shift_accepted_after_three_observations():
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 1.0))
    results = feed(tracker, [(75, 21.0), (90, 21.01), (105, 21.02)])
    assert [r.accepted for r in results] == [False, False, True]
    assert results[-1].level_shift
    assert tracker.latest.level == 21.02
    assert all(o.level > 20 for o in tracker.history)


def test_slow_gauge_large_drop_rejected():
    """bomflood-db4: 3-hourly gauge, -26.34 after 0.91 must not pass."""
    tracker = A.GaugeTracker()
    feed(tracker, [(0, 0.91), (180, 0.91), (360, 0.91)])
    assert not tracker.ingest(obs(540, -26.34)).accepted


def test_long_outage_large_drop_rejected():
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 0.91))
    assert not tracker.ingest(obs(60 + 180, -26.34)).accepted


def test_readings_a_minute_apart_during_real_rise():
    """bomflood-db4: 0.23 m in 1 min during a fast rise is not implausible."""
    tracker = A.GaugeTracker()
    feed(tracker, rising(0, 60, 15, 1.0, 8.0))
    assert tracker.ingest(obs(61, tracker.latest.level + 0.23)).accepted


def test_out_of_order_reading_does_not_bypass_screening():
    """bomflood-ho8."""
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 1.0))
    assert not tracker.ingest(obs(50, 9.5)).accepted
    assert max(o.level for o in tracker.history) == 1.0


def test_same_timestamp_revision_screened():
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 1.0))
    assert not tracker.ingest(obs(60, -26.34)).accepted
    assert tracker.latest.level == 1.0
    assert tracker.ingest(obs(60, 1.02)).accepted
    assert tracker.latest.level == 1.02


def test_backfill_fits_neighbours():
    tracker = A.GaugeTracker()
    feed(tracker, [(0, 1.0), (60, 1.2)])
    assert tracker.ingest(obs(30, 1.1)).accepted
    assert [o.level for o in tracker.history] == [1.0, 1.1, 1.2]
    assert not tracker.ingest(obs(45, 9.0)).accepted


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


def test_rate_needs_half_an_hour_of_readings():
    tracker = A.GaugeTracker()
    feed(tracker, [(0, 1.0), (15, 1.1)])
    assert tracker.rate() is None
    tracker.ingest(obs(30, 1.2))
    assert tracker.rate().rate_m_per_h == pytest.approx(0.4)


def test_rate_tracks_steady_rise():
    tracker = A.GaugeTracker()
    feed(tracker, rising(0, 120, 15, 1.0, 0.5))
    assert tracker.rate().rate_m_per_h == pytest.approx(0.5, abs=0.001)


def test_one_bad_sample_inside_window_does_not_fake_a_rise():
    """bomflood-fmi: a dropout (here a small one that passes screening) must
    not produce a false rising projection when it becomes the oldest sample."""
    tracker = A.GaugeTracker()
    series = steady(0, 240, 15, 1.71)
    series[8] = (series[8][0], 1.30)  # within plausibility, still wrong
    feed(tracker, series)
    for minute in range(0, 121, 15):
        sub = A.GaugeTracker()
        feed(sub, [p for p in series if p[0] <= 120 + minute])
        rate = sub.rate()
        assert rate is None or abs(rate.rate_m_per_h) < 0.05, (minute, rate)


def test_slow_gauge_gets_a_rate():
    """bomflood-jrx: 3-hourly gauges must still get a rate."""
    tracker = A.GaugeTracker()
    feed(tracker, [(0, 1.0), (180, 1.3), (360, 1.6), (540, 1.9)])
    rate = tracker.rate()
    assert rate is not None and rate.rate_m_per_h == pytest.approx(0.1, abs=0.001)


def test_level_shift_resets_rate_basis():
    """bomflood-db4/3ys: old-datum readings must not mix into the new rate."""
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 1.0))
    feed(tracker, [(75, 21.0), (90, 21.0), (105, 21.0)])
    assert tracker.rate().rate_m_per_h == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_stale_limits_follow_cadence():
    fast = A.GaugeTracker()
    feed(fast, steady(0, 120, 15, 1.0))
    assert fast.stale_after_hours() == 1.0
    slow = A.GaugeTracker()
    feed(slow, [(0, 1.0), (180, 1.0), (360, 1.0), (540, 1.0)])
    # 3 x cadence would be 9 h; capped so a dead feed is noticed within 6 h.
    assert slow.stale_after_hours() == 6.0


def test_stale_limit_follows_publication_not_logging():
    """Canungra, Oct 2026: logs every ~5 min in rain, BOM publishes hourly ~45 min late.

    Judged by cadence it was out of date for half of every hour.
    """
    tracker = A.GaugeTracker()
    feed(tracker, [(m, 1.0 + m / 1000) for m in range(0, 120, 5)])
    assert tracker.stale_after_hours() == 1.0  # cadence-based, before any publications
    for hour in range(3):
        # Newest reading at hh:55, next batch published at (hh+1):45.
        tracker.note_arrival(obs(55 + 60 * hour, 1.0), at(105 + 60 * hour))
    assert tracker.stale_after_hours() == pytest.approx(1.5 * 50 / 60, abs=0.01)
    # Survives a restart.
    again = A.GaugeTracker.from_dict(tracker.to_dict())
    assert again.stale_after_hours() == tracker.stale_after_hours()


def test_stale_limit_from_publications_is_capped():
    tracker = A.GaugeTracker()
    for _ in range(5):
        tracker.note_arrival(obs(0, 1.0), at(10 * 60))
    assert tracker.stale_after_hours() == 6.0
    tracker.note_arrival(obs(0, 1.0), at(-5))  # nonsense ages are ignored
    assert len(tracker.arrival_ages) == 5


# ---------------------------------------------------------------------------
# Single-reading spikes
# ---------------------------------------------------------------------------


def test_one_off_spike_is_never_shown():
    """Wolffdene, Oct 2026: 1.80 -> 2.65 -> 1.80 m."""
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 1.80))
    held = tracker.ingest(obs(75, 2.65))
    assert held.held and not held.accepted
    assert tracker.latest.level == 1.80
    assert tracker.ingest(obs(90, 1.80)).accepted
    assert [o.level for o in tracker.history] == [1.80] * 6
    assert tracker.held is None


def test_big_spike_on_slow_gauge_is_dropped():
    """Plunkett Road, Oct 2026: 4.35 m against a real peak of 1.05 m."""
    tracker = A.GaugeTracker()
    feed(tracker, [(0, 0.80), (60, 0.82), (120, 0.85), (180, 4.35), (240, 0.90)])
    assert max(o.level for o in tracker.history) == 0.90


def test_real_jump_is_confirmed_by_next_reading():
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 1.0))
    assert tracker.ingest(obs(75, 2.0)).held
    assert tracker.ingest(obs(90, 2.3)).accepted
    assert [o.level for o in tracker.history][-2:] == [2.0, 2.3]


def test_held_reading_repeated_on_later_polls_stays_held():
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 60, 15, 1.0))
    for _ in range(3):
        assert tracker.ingest(obs(75, 2.0)).held
    assert tracker.latest.level == 1.0


def test_spike_in_backfill_and_seed_is_dropped():
    series = [obs(m, 1.8) for m in range(0, 120, 15)]
    series[4] = obs(60, 2.65)
    assert max(o.level for o in A.screen_series(series)) == 1.8
    tracker = A.GaugeTracker()
    feed(tracker, [(0, 1.8), (15, 1.8), (45, 1.8)])
    assert not tracker.ingest(obs(30, 2.65)).accepted
    assert tracker.ingest(obs(30, 1.81)).accepted
    new = A.GaugeTracker()
    new.ingest(obs(0, 1.0))
    assert new.stale_after_hours() == 3.0


def test_is_stale():
    tracker = A.GaugeTracker()
    feed(tracker, steady(0, 120, 15, 1.0))
    assert not tracker.is_stale(at(150))
    assert tracker.is_stale(at(190))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_round_trip():
    tracker = A.GaugeTracker(datum="LGH")
    feed(tracker, rising(0, 60, 15, 1.0, 0.4))
    again = A.GaugeTracker.from_dict(tracker.to_dict())
    assert again.history == tracker.history and again.datum == "LGH"


def test_restore_drops_bad_oldest_reading():
    """bomflood-edg: stored [-26.34, 0.91 x7] must keep the 0.91s."""
    data = {"observations": [[at(0).isoformat(), -26.34]] + [
        [at(15 * i).isoformat(), 0.91] for i in range(1, 8)
    ]}
    tracker = A.GaugeTracker.from_dict(data)
    assert [o.level for o in tracker.history] == [0.91] * 7


@pytest.mark.parametrize(
    "data",
    [None, [], "x", {"observations": None}, {"observations": [None, [1], ["bad", 1.0], [at(0).isoformat(), "x"],
     ["2026-10-19T00:00:00", 1.0], [at(0).isoformat(), True]]}],
)
def test_restore_tolerates_corrupt_storage(data):
    """bomflood-3p1: corrupt storage must never raise."""
    assert A.GaugeTracker.from_dict(data).history == []


def test_restore_legacy_single_observation_format():
    tracker = A.GaugeTracker.from_dict({"timestamp": at(0).isoformat(), "level": 1.5})
    assert tracker.latest.level == 1.5


# ---------------------------------------------------------------------------
# Rainfall
# ---------------------------------------------------------------------------


def test_rainfall_too_old_is_dropped():
    """bomflood-b78."""
    rec = A.HcsRecord("1", at(0), 13.0, "mm", "", 3600, 3)
    assert A.fresh_rainfall(rec, at(90)) is rec
    assert A.fresh_rainfall(rec, at(150)) is None


def test_earliest_time_is_earlier_and_monotonic():
    """The calibrated earliest time is before the straight line and never
    shows an earlier time for a longer projection."""
    assert A.earliest_hours(None) is None
    previous = 0.0
    for tenth in range(1, 481):
        projected = tenth / 10
        earliest = A.earliest_hours(projected)
        assert 0.1 <= earliest <= projected
        assert earliest >= previous - 0.1  # rounding to 0.1 h
        previous = earliest


# ---------------------------------------------------------------------------
# BOM flood warnings
# ---------------------------------------------------------------------------

WARNING = (pathlib.Path(__file__).parent / "fixtures" / "IDQ20885.txt").read_text()


def test_parse_flood_warning():
    w = A.parse_flood_warning("IDQ20885", WARNING)
    assert w.title == "Major Flood Warning for the Logan and Albert Rivers"
    assert w.severity == "major"
    assert w.issued.startswith("4:52 am EST")
    assert "Latest River Heights" not in w.body
    assert w.url.endswith("IDQ20885.html")


def test_forecast_for_gauge_with_peak():
    w = A.parse_flood_warning("IDQ20885", WARNING)
    f = A.gauge_forecast(w, "Logan River at Waterford")
    assert f.level_m == 13.5
    assert f.flood_class == "major"
    assert f.when == "Monday afternoon"
    assert len(f.sentences) == 2


def test_forecast_remain_above():
    w = A.parse_flood_warning("IDQ20885", WARNING)
    f = A.gauge_forecast(w, "Albert River at Wolffdene")
    assert f.level_m == 10.0 and f.flood_class == "major" and f.when == "during Monday"


def test_mentioned_without_forecast():
    w = A.parse_flood_warning("IDQ20885", WARNING)
    f = A.gauge_forecast(w, "Albert River at Bromfleet")
    assert f is not None and f.level_m is None
    assert "peaked at 15.80 metres" in f.sentences[0]


def test_unmentioned_gauge_and_table_only_mentions():
    w = A.parse_flood_warning("IDQ20885", WARNING)
    assert A.gauge_forecast(w, "Coomera River at Canungra Army") is None


def test_marine_and_weather_warnings_ignored():
    marine = "IDT20100\nAustralian Government Bureau of Meteorology\nTasmania\n\nMarine Wind Warning Summary for Tasmania\n"
    assert A.parse_flood_warning("IDT20100", marine) is None
    watch = "IDN36501\nAustralian Government Bureau of Meteorology\nNew South Wales\n\nFlood Watch for the Hawkesbury River\nIssued at 1:00 pm\n"
    assert A.parse_flood_warning("IDN36501", watch).title == "Flood Watch for the Hawkesbury River"


def test_warning_file_names():
    assert A.WARNING_FILE_RE.match("IDQ20885.txt")
    assert A.WARNING_FILE_RE.match("IDN36501.txt")
    assert not A.WARNING_FILE_RE.match("IDQ65404.html")
    assert not A.WARNING_FILE_RE.match("IDZ65910_20261019.hcs")


# Real BOM warnings captured by tools/capture_warnings.py (October 2026).

def _real(name: str) -> A.FloodWarning:
    text = (pathlib.Path(__file__).parent / "fixtures" / f"warnings_{name}.txt").read_text()
    return A.parse_flood_warning(name.split("_")[0], text)


def test_real_titles_and_severity():
    assert _real("IDQ20827_condamine").title == "Initial Minor Flood Warning for the Upper Condamine River"
    assert _real("IDQ20827_condamine").severity == "minor"
    assert _real("IDQ20795_maroochy").severity is None  # "Initial Flood Warning" names no class


def test_real_thunderstorm_warnings_rejected():
    assert _real("IDQ21035_thunderstorm_cancel") is None
    assert _real("IDS21033_thunderstorm") is None


def test_real_may_exceed_then_the_river_may_reach():
    """'may exceed the minor flood level (5.00 m)' then 'The river may reach around 5.20 m'."""
    f = A.gauge_forecast(_real("IDQ20827_condamine"), "Condamine River at Warwick")
    assert (f.level_m, f.flood_class, f.when, f.direction) == (5.2, "minor", "Friday morning", "rise")
    assert any(s.startswith("The river may reach around 5.20 m") for s in f.sentences)
    assert not any(s.endswith(":") for s in f.sentences)  # section headings are not sentences


def test_real_continuation_stops_at_next_gauge():
    w = _real("IDQ20827_condamine")
    pratten = A.gauge_forecast(w, "Condamine River at Pratten")
    assert (pratten.level_m, pratten.when) == (5.5, "Friday afternoon")
    assert not any("Tummaville" in s for s in pratten.sentences)
    tumma = A.gauge_forecast(w, "Condamine River at Tummaville")
    assert (tumma.level_m, tumma.flood_class, tumma.when) == (6.0, "minor", "Friday evening")
    assert A.gauge_forecast(w, "Condamine River at Killarney") is None  # only in the height table


def test_real_peak_just_below_minor_is_not_minor():
    f = A.gauge_forecast(_real("IDQ20845_warrego_1"), "Warrego River at Charleville")
    assert (f.level_m, f.flood_class, f.direction) == (3.7, None, "peak")
    f = A.gauge_forecast(_real("IDQ20845_warrego_2"), "Warrego River at Charleville")
    assert (f.level_m, f.flood_class, f.when) == (4.0, None, "Friday afternoon")  # "peak near"


def test_real_likely_to_exceed_threshold():
    f = A.gauge_forecast(_real("IDQ20845_warrego_1"), "Warrego River at Bakers Bend")
    assert (f.level_m, f.flood_class, f.direction) == (5.0, "minor", "rise")
    assert f.when == "during Saturday 24 October"
    f = A.gauge_forecast(_real("IDQ20845_warrego_2"), "Warrego River at Bakers Bend")
    assert (f.level_m, f.flood_class, f.when) == (5.6, "minor", "during Saturday")


def test_real_highest_forecast_is_reported():
    f = A.gauge_forecast(_real("IDQ20855_bulloo"), "Bulloo River at Quilpie")
    assert (f.level_m, f.flood_class, f.when) == (3.4, "minor", "Thursday evening")
    assert any("Wednesday evening" in s for s in f.sentences)  # first crossing kept in the text


def test_real_peak_above_and_fall_below():
    f = A.gauge_forecast(_real("IDQ20860_barcoo_1"), "Barcoo River at Blackall")
    assert (f.level_m, f.flood_class, f.direction) == (2.0, "minor", "peak")
    f = A.gauge_forecast(_real("IDQ20860_barcoo_4"), "Barcoo River at Blackall")
    assert (f.level_m, f.flood_class, f.when, f.direction) == (2.0, None, "during Thursday evening", "fall")


def test_real_warning_without_gauge_forecasts():
    w = _real("IDQ20795_maroochy")
    assert A.gauge_forecast(w, "Mooloolah River at Mooloolah") is None  # table only


def test_rolling_hour_rainfall():
    end = at(0)
    q = {end - timedelta(minutes=15 * i): v for i, v in enumerate([0.4, 1.0, 2.0, 3.0, 9.0])}
    record, last = A.rolling_hour_rainfall("1", q, end + timedelta(minutes=5))
    assert record.value == 6.4 and record.observed_at == end and last == 0.4
    assert A.rolling_hour_rainfall("1", q, end + timedelta(hours=3)) == (None, None)  # too old
    del q[end - timedelta(minutes=30)]
    assert A.rolling_hour_rainfall("1", q, end) == (None, None)  # gap: no undercount
    assert A.rolling_hour_rainfall("1", {}, end) == (None, None)


def test_real_major_warning_mary_river():
    w = _real("IDQ20790_mary_major")
    assert w.severity == "major" and not w.final
    tiaro = A.gauge_forecast(w, "Mary River at Tiaro")
    assert (tiaro.level_m, tiaro.flood_class, tiaro.when) == (12.0, "major", "overnight Sunday into Monday")
    miva = A.gauge_forecast(w, "Mary River at Miva")
    assert (miva.level_m, miva.flood_class, miva.direction) == (9.5, "moderate", "peak")
    port = A.gauge_forecast(w, "Mary River at Maryborough (Portside)")
    assert (port.level_m, port.flood_class, port.when) == (5.6, "minor", "Monday evening")
    dagun = A.gauge_forecast(w, "Mary River at Dagun Pocket")
    assert (dagun.flood_class, dagun.direction) == ("minor", "remain")


def test_real_final_warning_and_creek_continuation():
    w = _real("IDQ20809_bremer_final")
    assert w.final and w.severity is None
    amberley = A.gauge_forecast(w, "Warrill Creek at Amberley")
    assert any(s.startswith("The creek is currently at 3.52 m") for s in amberley.sentences)
    assert (amberley.flood_class, amberley.direction) == (None, "fall")
    rosewood = A.gauge_forecast(w, "Bremer River at Rosewood")
    assert (rosewood.flood_class, rosewood.direction) == (None, "remain")


def _w(title: str, body: str = "", pid: str = "IDQ20885") -> A.FloodWarning:
    return A.FloodWarning(pid, title, None, None, body)


@pytest.mark.parametrize(
    ("gauge", "title", "expected"),
    [
        ("Albert River at Tamborine", "Major Flood Warning for the Logan and Albert Rivers", True),
        ("Logan River at Logan Village", "Major Flood Warning for the Logan and Albert Rivers", True),
        ("Coomera River at Canungra Army", "Flood Warning for the Logan, Albert and Coomera Rivers", True),
        ("Coomera River at Canungra Army", "Minor Flood Warning for the Coomera River", True),
        ("Coomera River at Canungra Army", "Major Flood Warning for the Logan and Albert Rivers", False),
        ("Cedar Creek at Plunkett Road", "Flood Warning for the Pine and Caboolture Rivers", False),
        ("Cedar Creek at Plunkett Road", "Flood Warning for Cedar Creek", True),
        ("Albert River at Wolffdene", "Flood Warning for the Albert Creek", False),
        ("Slacks Creek", "Flood Warning for Slacks Creek", True),
        ("Mt Crosby Weir", "Flood Warning for the Brisbane River", False),
    ],
)
def test_warning_names_river(gauge, title, expected):
    assert A.warning_names_river(_w(title), gauge) is expected


def test_river_match_uses_title_only():
    w = _w("Flood Warning for the Pine and Caboolture Rivers", "Cedar Creek at Bunya is rising.")
    assert not A.warning_names_river(w, "Cedar Creek at Plunkett Road")


def test_flood_watch_lists_catchments():
    w = A.parse_flood_warning(
        "IDQ20800",
        "IDQ20800\nBureau\n\nFlood Watch for South East Queensland Coastal Catchments\n\n"
        "Issued at 4:05 pm\n\nCatchments likely to be affected include:\nNoosa River\n"
        "Logan and Albert Rivers\nCoomera River\n",
    )
    assert w is not None and w.watch and not w.final
    assert A.watch_covers_river(w, "Albert River at Tamborine")
    assert A.watch_covers_river(w, "Coomera River at Canungra Army")
    assert not A.watch_covers_river(w, "Mary River at Gympie")


def test_title_must_start_with_warning_type():
    text = "IDQ20999\nBureau\n\nRefer to Flood Warning products for river details.\n\nIssued at 1 pm\n"
    assert A.parse_flood_warning("IDQ20999", text) is None
    ok = "IDQ20999\nBureau\n\nInitial Moderate Flood Warning for the Bremer River\n\nIssued at 1 pm\n"
    assert A.parse_flood_warning("IDQ20999", ok).title.startswith("Initial Moderate")


# ---------------------------------------------------------------------------
# Tides
# ---------------------------------------------------------------------------


def tide(m: float, mean: float = 1.5, amp: float = 0.35) -> float:
    return round(mean + amp * math.sin(2 * math.pi * (m / 60) / 12.42), 3)


def feed_tide(tracker, start, end, step=15, **kw):
    for m in range(start, end, step):
        tracker.ingest(obs(m, tide(m, **kw)))
        tracker.update_tide()


def test_tide_detected_after_a_day_of_matches():
    tracker = A.GaugeTracker()
    feed_tide(tracker, 0, 30 * 60)
    assert not tracker.tidal  # not enough history to fit yet
    feed_tide(tracker, 30 * 60, 53 * 60)
    assert not tracker.tidal and tracker.tide_since is not None  # matching, not yet for 24 h
    feed_tide(tracker, 53 * 60, 55 * 60)
    assert tracker.tidal
    assert tracker.tide_range_m == pytest.approx(0.7, abs=0.05)
    again = A.GaugeTracker.from_dict(tracker.to_dict())
    assert again.tidal and again.tide_range_m == tracker.tide_range_m


def test_river_rise_is_not_a_tide():
    tracker = A.GaugeTracker()
    for m in range(0, 45 * 60, 15):
        h = m / 60
        level = 1.0 + (3.0 * math.exp(-((h - 25) / 6) ** 2))  # a flood hydrograph
        tracker.ingest(obs(m, round(level, 3)))
        tracker.update_tide()
    assert not tracker.tidal and tracker.tide_since is None


def test_flood_swamping_tide_turns_tidal_off():
    tracker = A.GaugeTracker()
    feed_tide(tracker, 0, 56 * 60)
    assert tracker.tidal
    for m in range(56 * 60, 96 * 60, 15):
        h = m / 60 - 56
        tracker.ingest(obs(m, round(tide(m) + 4.0 * (1 - math.exp(-h / 8)), 3)))
        tracker.update_tide()
    assert not tracker.tidal


def test_within_tide_band():
    tracker = A.GaugeTracker()
    feed_tide(tracker, 0, 56 * 60)
    assert tracker.within_tide(at(56 * 60))
    lo, hi = tracker.tide_band()
    assert hi == pytest.approx(1.85, abs=0.01) and lo == pytest.approx(1.15, abs=0.01)
    level = tracker.latest.level
    m = 56 * 60
    while level <= hi + 0.3:  # the river rises above every recent high tide
        m += 15
        level += 0.1
        assert tracker.ingest(obs(m, round(level, 3))).accepted
    assert not tracker.within_tide(at(m))


def test_tide_fit_needs_thirty_hours():
    assert A.tide_fit([obs(m, tide(m)) for m in range(0, 20 * 60, 15)]) is None
    fit, rng = A.tide_fit([obs(m, tide(m)) for m in range(0, 36 * 60, 15)])
    assert fit > 0.95 and rng == pytest.approx(0.7, abs=0.05)
