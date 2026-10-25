"""Replay real historical floods through the integration's logic.

Usage:
    python tools/backtest_floods.py EVENTS_DIR [--json OUT]

EVENTS_DIR contains:
    meta.json            {fwo_id: {name, minor, moderate, major, wdo_last, level_now, ...}}
    <fwo_id>_<event>.json  [[iso timestamp, level, quality], ...] from BOM Water
                         Data Online for the same physical gauge.

Only gauges whose Water Data Online level matches BOM's current flood-warning
level (same datum) are used, so BOM's flood thresholds apply.

The replay polls every 15 minutes, as the integration does, feeding each
reading once its timestamp has passed. At each poll it records what the user
would see: status, trend, and hours to each flood level. Those are scored
against what the river actually did afterwards.

Scores:
    status          displayed status equals the true status at poll time
    warning_recall  threshold crossings preceded by a projection of that level
                    at least LEAD_H hours ahead (or since the rise began)
    timing          projections made 0.5-6 h before a crossing whose projected
                    time is within max(TIMING_ABS_H, TIMING_REL x actual) of it
    not_late        those projections that are not later than actual + tolerance
    false_alarm     projections of a crossing within 6 h where none happened
                    within 12 h
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import itertools

from custom_components.bom_flood import analysis as A

POLL = timedelta(minutes=15)
DATUM_TOLERANCE_M = 0.15
TIMING_ABS_H = 0.5
TIMING_REL = 0.3
LEAD_H = 1.0
LEVELS = ("minor", "moderate", "major")


def load(events_dir: str):
    meta = json.load(open(os.path.join(events_dir, "meta.json")))
    out = []
    for path in sorted(glob.glob(os.path.join(events_dir, "*_*.json"))):
        name = os.path.basename(path)[:-5]
        sid, event = name.rsplit("_", 1)
        m = meta.get(sid)
        if not m or m.get("wdo_last") is None or m.get("level_now") is None:
            continue
        if abs(m["wdo_last"] - m["level_now"]) > DATUM_TOLERANCE_M:
            continue
        thresholds = {k: m.get(k) for k in LEVELS}
        if all(v is None for v in thresholds.values()):
            continue
        raw = json.load(open(path))
        series = []
        for row in raw:
            ts, value = row[0], row[1]
            when = A.parse_utc(ts)
            if when is not None and value is not None:
                series.append(A.Observation(when, float(value)))
        series.sort(key=lambda o: o.observed_at)
        if len(series) < 12:
            continue
        out.append((sid, m.get("name", sid), event, thresholds, series))
    return out


def true_level(series, when):
    """Level at `when` by linear interpolation (None outside the data)."""
    lo, hi = 0, len(series) - 1
    if when < series[0].observed_at or when > series[-1].observed_at:
        return None
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if series[mid].observed_at <= when:
            lo = mid
        else:
            hi = mid
    a, b = series[lo], series[hi]
    if b.observed_at == a.observed_at:
        return a.level
    if (b.observed_at - a.observed_at) > timedelta(hours=3):
        return None
    f = (when - a.observed_at) / (b.observed_at - a.observed_at)
    return a.level + f * (b.level - a.level)


def crossings(series, threshold):
    """Times the true series first reaches `threshold` from below (interpolated)."""
    out = []
    for a, b in itertools.pairwise(series):
        if a.level < threshold <= b.level:
            f = (threshold - a.level) / (b.level - a.level)
            out.append(a.observed_at + f * (b.observed_at - a.observed_at))
    return out


def replay(series, thresholds):
    """Yield (poll time, displayed level, status, rate, hours_to dict, stale)."""
    tracker = A.GaugeTracker()
    i = 0
    t = series[0].observed_at + POLL
    end = series[-1].observed_at
    while t <= end:
        while i < len(series) and series[i].observed_at <= t:
            tracker.ingest(series[i])
            i += 1
        latest = tracker.latest
        if latest is not None:
            stale = tracker.is_stale(t)
            rate = None if stale else (r.rate_m_per_h if (r := tracker.rate()) else None)
            status = A.derive_flood_status(latest.level, thresholds["minor"], thresholds["moderate"], thresholds["major"])
            hours = {k: A.hours_to_level(latest.level, rate, thresholds[k]) for k in LEVELS}
            yield t, latest, status, rate, hours, stale
        t += POLL


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("events_dir")
    ap.add_argument("--json")
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()

    data = load(args.events_dir)
    st_ok = st_n = 0
    cross_n = cross_warned = 0
    lead_times: list[float] = []
    tim_n = tim_ok = tim_not_late = 0
    early_n = early_ok = 0
    fa_n = fa_bad = 0
    per_event = defaultdict(lambda: [0, 0])
    examples: dict[str, list[str]] = defaultdict(list)
    gauges_with_crossings = set()
    miss_reasons: dict[str, int] = defaultdict(int)

    for sid, name, event, thresholds, series in data:
        polls = list(replay(series, thresholds))
        cross_times = {k: crossings(series, thresholds[k]) for k in LEVELS if thresholds[k] is not None}

        for t, latest, status, rate, hours, stale in polls:
            actual = true_level(series, t)
            if actual is not None:
                truth = A.derive_flood_status(actual, thresholds["minor"], thresholds["moderate"], thresholds["major"])
                st_n += 1
                st_ok += truth == status

            for level, times in cross_times.items():
                upcoming = [c for c in times if c > t]
                next_cross = upcoming[0] if upcoming else None
                proj = hours.get(level)
                # Timing of projections shortly before a real crossing.
                if next_cross is not None:
                    actual_h = (next_cross - t).total_seconds() / 3600
                    if 0.5 <= actual_h <= 6 and latest.level < thresholds[level]:
                        tim_n += 1
                        tol = max(TIMING_ABS_H, TIMING_REL * actual_h)
                        if proj is not None:
                            early_n += 1
                            # One poll interval of slack: the next poll would show it.
                            early_ok += actual_h >= A.earliest_hours(proj) - 0.25
                        if proj is not None and abs(proj - actual_h) <= tol:
                            tim_ok += 1
                            miss_reasons["within tolerance"] += 1
                        elif stale:
                            miss_reasons["gauge out of date"] += 1
                        elif rate is None:
                            miss_reasons["no rate yet"] += 1
                        elif rate <= A.TREND_THRESHOLD_M_PER_H:
                            miss_reasons["not rising yet at that moment"] += 1
                        elif proj is None:
                            miss_reasons["rising too slowly to project"] += 1
                        elif proj > actual_h:
                            miss_reasons["late: river sped up"] += 1
                        else:
                            miss_reasons["early: river slowed"] += 1
                        if proj is not None and proj <= actual_h + tol:
                            tim_not_late += 1
                        elif len(examples["late_or_missing"]) < args.show:
                            examples["late_or_missing"].append(
                                f"{event} {sid} {name} {level}: at {t:%m-%d %H:%M} level {latest.level:.2f} "
                                f"rate {rate} projected {proj} actual {actual_h:.1f} h"
                            )
                # False alarms: projected within 6 h, nothing within 12 h.
                if proj is not None and proj <= 6:
                    fa_n += 1
                    if next_cross is None or (next_cross - t) > timedelta(hours=12):
                        fa_bad += 1
                        if len(examples["false_alarm"]) < args.show:
                            examples["false_alarm"].append(
                                f"{event} {sid} {name} {level}: at {t:%m-%d %H:%M} level {latest.level:.2f} "
                                f"rate {rate} projected {proj} h; no crossing within 12 h"
                            )

        for level, times in cross_times.items():
            for c in times:
                # Skip crossings at the very start of the data (no chance to warn).
                if c - series[0].observed_at < timedelta(hours=3):
                    continue
                cross_n += 1
                gauges_with_crossings.add(sid)
                warned_at = [t for t, latest, *_rest in polls if t < c and _rest[2].get(level) is not None and latest.level < thresholds[level]]
                first = None
                # The earliest poll of an uninterrupted warning run ending at the crossing.
                for t, latest, status, rate, hours, stale in reversed([p for p in polls if p[0] < c]):
                    if hours.get(level) is None or latest.level >= thresholds[level]:
                        break
                    first = t
                lead = (c - first).total_seconds() / 3600 if first else 0.0
                lead_times.append(lead)
                ok = lead >= LEAD_H or (warned_at and _rise_duration(series, c) < LEAD_H + 0.25)
                cross_warned += bool(ok)
                per_event[event][0] += 1
                per_event[event][1] += bool(ok)
                if not ok and len(examples["unwarned"]) < args.show:
                    examples["unwarned"].append(f"{event} {sid} {name} {level} at {c:%m-%d %H:%M}: lead {lead:.2f} h")

    lead_sorted = sorted(lead_times)
    report = {
        "series": len(data),
        "gauges_with_crossings": len(gauges_with_crossings),
        "status": {"pct": _pct(st_ok, st_n), "n": st_n},
        "warning_recall": {"pct": _pct(cross_warned, cross_n), "n": cross_n,
                           "median_lead_h": lead_sorted[len(lead_sorted) // 2] if lead_sorted else None},
        "earliest_time_not_too_late": {"pct": _pct(early_ok, early_n), "n": early_n,
                                       "note": "in-sample; held-out estimate in const.py"},
        "projection_shown_before_crossing": {"pct": _pct(early_n, tim_n), "n": tim_n},
        "timing_within_tolerance": {"pct": _pct(tim_ok, tim_n), "n": tim_n},
        "timing_not_late": {"pct": _pct(tim_not_late, tim_n), "n": tim_n},
        "false_alarm_rate": {"pct": _pct(fa_bad, fa_n), "n": fa_n},
        "timing_breakdown_pct": {k: _pct(v, tim_n) for k, v in sorted(miss_reasons.items(), key=lambda kv: -kv[1])},
        "by_event": {k: {"crossings": v[0], "warned_pct": _pct(v[1], v[0])} for k, v in sorted(per_event.items())},
    }
    print(json.dumps(report, indent=2))
    for k, lines in examples.items():
        print(f"\n-- {k} --")
        for line in lines:
            print("  " + line)
    if args.json:
        json.dump({"report": report, "examples": examples}, open(args.json, "w"), indent=2)
    return 0


def _rise_duration(series, crossing) -> float:
    """Hours from the start of the final rise to the crossing."""
    before = [o for o in series if o.observed_at <= crossing]
    start = before[-1]
    for o in reversed(before[:-1]):
        if o.level > start.level + 0.005:
            break
        start = o
    return (crossing - start.observed_at).total_seconds() / 3600


def _pct(a, b):
    return round(100 * a / b, 2) if b else None


if __name__ == "__main__":
    raise SystemExit(main())
