"""Score the integration's logic against BOM's own published data.

Usage:
    python tools/validate.py DATA_DIR [--json OUT]

DATA_DIR holds files mirrored from ftp://ftp.bom.gov.au/anon/gen/fwo/:
    IDZ65910_*.hcs   latest water level per gauge (every 15 min)
    IDZ65911_*.hcs   every water level reading for ~3 h (hourly)
    maps/<stamp>/IDx65xxx.html  regional flood maps (BOM's displayed level,
                     status and trend for every gauge)

Checks, each scored as a percentage:
    fidelity   value + timestamp we take from IDZ65910 equal the raw rows
    map_level  our level equals BOM's displayed map level at the same time
    status     our flood status equals BOM's displayed status
    trend      our trend (from replayed history) equals BOM's displayed trend
    forecast   level projected 1 h ahead from our rate is within tolerance of
               what the gauge actually read 1 h later (rising gauges only)

The tool uses the integration's own analysis module, so it measures exactly
the code that ships.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from custom_components.bom_flood import analysis as A
from custom_components.bom_flood.const import TREND_RISING

MAP_TZ = {
    "IDQ": ZoneInfo("Australia/Brisbane"),
    "IDN": ZoneInfo("Australia/Sydney"),
    "IDV": ZoneInfo("Australia/Melbourne"),
    "IDS": ZoneInfo("Australia/Adelaide"),
    "IDT": ZoneInfo("Australia/Hobart"),
}

# A 1 h projection counts as accurate if within this of the actual level:
# the larger of an absolute floor (gauge resolution plus noise) and a share of
# the change that actually happened.
FORECAST_ABS_TOL_M = 0.05
FORECAST_REL_TOL = 0.25


def file_time(path: str) -> datetime:
    stamp = os.path.basename(path).split("_")[1].split(".")[0]
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def raw_latest(path: str) -> dict[str, tuple[str, str]]:
    """Independent naive parse: last WL row per station as raw strings."""
    out: dict[str, tuple[str, str]] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        rows = csv.reader(line for line in fh if not line.startswith("#"))
        for row in rows:
            if len(row) >= 11 and row[1] == "WL":
                prev = out.get(row[4])
                if prev is None or row[5] >= prev[1]:
                    out[row[4]] = (row[6], row[5])
    return out


def load_maps(data_dir: str) -> list[tuple[str, str, list[A.MapStation]]]:
    snapshots = []
    for path in sorted(glob.glob(os.path.join(data_dir, "maps", "*", "*.html"))):
        stamp = os.path.basename(os.path.dirname(path))
        product = os.path.basename(path)[:-5]
        with open(path, encoding="utf-8", errors="replace") as fh:
            snapshots.append((stamp, product, A.parse_region_map(fh.read())))
    return snapshots


def map_time(product: str, local: str) -> datetime | None:
    try:
        naive = datetime.strptime(local, "%d-%m-%Y %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=MAP_TZ[product[:3]]).astimezone(UTC)


def load_timeline(data_dir: str) -> dict[str, list[A.Observation]]:
    """Every WL observation seen in any IDZ65910/IDZ65911 file, per gauge."""
    merged: dict[str, dict[datetime, float]] = defaultdict(dict)
    paths = sorted(glob.glob(os.path.join(data_dir, "IDZ6591[01]_*.hcs")))
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            try:
                parsed = A.parse_hcs(fh.read(), "WL")
            except A.HcsFormatError:
                continue
        for sid, rows in parsed.records.items():
            for r in rows:
                if r.value is not None:
                    merged[sid][r.observed_at] = r.value
    return {
        sid: [A.Observation(t, v) for t, v in sorted(series.items())]
        for sid, series in merged.items()
    }


def level_at(series: list[A.Observation], when: datetime) -> float | None:
    """Linear interpolation inside the series, None outside it."""
    times = [o.observed_at for o in series]
    i = bisect.bisect_left(times, when)
    if i < len(times) and times[i] == when:
        return series[i].level
    if i == 0 or i == len(times):
        return None
    a, b = series[i - 1], series[i]
    gap = (b.observed_at - a.observed_at).total_seconds()
    if gap > 3 * 3600:
        return None
    frac = (when - a.observed_at).total_seconds() / gap
    return a.level + frac * (b.level - a.level)


def tracker_at(series: list[A.Observation], when: datetime) -> A.GaugeTracker:
    tracker = A.GaugeTracker()
    for obs in series:
        if obs.observed_at > when:
            break
        tracker.ingest(obs)
    return tracker


def pct(ok: int, total: int) -> float | None:
    return round(100 * ok / total, 2) if total else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("--json")
    ap.add_argument("--show", type=int, default=8, help="examples of mismatches to print")
    args = ap.parse_args()
    d = args.data_dir
    report: dict[str, object] = {}
    examples: dict[str, list[str]] = defaultdict(list)

    # -- fidelity ----------------------------------------------------------
    wl_files = sorted(glob.glob(os.path.join(d, "IDZ65910_*.hcs")))
    ok = total = 0
    for path in wl_files:
        raw = raw_latest(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            parsed = A.parse_hcs(fh.read(), "WL")
        for sid, (value, ts) in raw.items():
            total += 1
            rec = parsed.latest(sid)
            good = (
                rec is not None
                and rec.observed_at == A.parse_utc(ts)
                and rec.value == A.safe_float(value)
            )
            ok += good
            if not good and len(examples["fidelity"]) < args.show:
                examples["fidelity"].append(f"{os.path.basename(path)} {sid} raw={value}@{ts} parsed={rec}")
    report["fidelity"] = {"pct": pct(ok, total), "n": total}

    # -- map comparisons ---------------------------------------------------
    timeline = load_timeline(d)
    snapshots = load_maps(d)
    lvl_ok = lvl_n = 0
    st_ok = st_n = 0
    st_e2e_ok = st_e2e_n = 0
    tr_ok = tr_n = 0
    tr_conf: Counter = Counter()
    tr_unknown = 0
    seen_status: set[tuple[str, str]] = set()
    for stamp, product, stations in snapshots:
        for m in stations:
            when = map_time(product, m.bom_observed_local)
            series = timeline.get(m.station_id)
            # Status from BOM's own displayed level isolates the classifier.
            if m.bom_status and m.bom_level is not None and (stamp, m.station_id) not in seen_status:
                seen_status.add((stamp, m.station_id))
                ours = A.derive_flood_status(m.bom_level, m.minor_m, m.moderate_m, m.major_m)
                st_n += 1
                st_ok += ours == m.bom_status
                if ours != m.bom_status and len(examples["status"]) < args.show:
                    examples["status"].append(
                        f"{product} {m.station_id} {m.name}: level {m.bom_level} thr "
                        f"{m.minor_m}/{m.moderate_m}/{m.major_m} BOM={m.bom_status} ours={ours}"
                    )
            if when is None or not series:
                continue
            exact = next((o for o in series if o.observed_at == when), None)
            if exact is not None and m.bom_level is not None:
                lvl_n += 1
                good = abs(exact.level - m.bom_level) <= 0.0101  # map shows 2 dp
                lvl_ok += good
                if not good and len(examples["map_level"]) < args.show:
                    examples["map_level"].append(
                        f"{product} {m.station_id} {m.name}: BOM map {m.bom_level} ours {exact.level} @ {when}"
                    )
                if m.bom_status:
                    tracker = tracker_at(series, when)
                    ours_level = tracker.latest.level if tracker.latest else None
                    ours = A.derive_flood_status(ours_level, m.minor_m, m.moderate_m, m.major_m)
                    st_e2e_n += 1
                    st_e2e_ok += ours == m.bom_status
                    if ours != m.bom_status and len(examples["status_e2e"]) < args.show:
                        examples["status_e2e"].append(
                            f"{product} {m.station_id} {m.name}: BOM {m.bom_status}@{m.bom_level} ours {ours}@{ours_level}"
                        )
            if m.bom_trend and when is not None:
                tracker = tracker_at(series, when)
                if tracker.latest is None or tracker.latest.observed_at != when:
                    continue
                rate = tracker.rate()
                ours = A.trend_from_rate(rate.rate_m_per_h if rate else None)
                if ours is None:
                    tr_unknown += 1
                    continue
                tr_n += 1
                tr_ok += ours == m.bom_trend
                tr_conf[(m.bom_trend, ours)] += 1
                if ours != m.bom_trend and len(examples["trend"]) < args.show:
                    recent = [round(o.level, 3) for o in tracker.history[-6:]]
                    examples["trend"].append(
                        f"{product} {m.station_id} {m.name}: BOM {m.bom_trend} ours {ours} "
                        f"rate={rate.rate_m_per_h if rate else None} recent={recent}"
                    )
    report["map_level"] = {"pct": pct(lvl_ok, lvl_n), "n": lvl_n}
    report["status"] = {"pct": pct(st_ok, st_n), "n": st_n}
    report["status_e2e"] = {"pct": pct(st_e2e_ok, st_e2e_n), "n": st_e2e_n}
    dangerous = tr_conf[("rising", "falling")] + tr_conf[("rising", "steady")]
    report["trend"] = {
        "pct": pct(tr_ok, tr_n),
        "n": tr_n,
        "unknown": tr_unknown,
        "bom_rising_missed": dangerous,
        "bom_rising_total": sum(v for (b, _), v in tr_conf.items() if b == "rising"),
        "confusion": {f"{b}->{o}": v for (b, o), v in sorted(tr_conf.items())},
    }

    # -- 1 h forecast backtest ---------------------------------------------
    fc_ok = fc_n = 0
    persist_ok = 0
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for sid, series in timeline.items():
        if len(series) < 6:
            continue
        start = series[0].observed_at
        end = series[-1].observed_at
        t = start + timedelta(hours=1)
        tracker = A.GaugeTracker()
        idx = 0
        while t + timedelta(hours=1) <= end:
            while idx < len(series) and series[idx].observed_at <= t:
                tracker.ingest(series[idx])
                idx += 1
            t_next = t + timedelta(minutes=15)
            latest = tracker.latest
            rate = tracker.rate()
            if latest is None or rate is None or tracker.is_stale(t):
                t = t_next
                continue
            if A.trend_from_rate(rate.rate_m_per_h) != TREND_RISING:
                t = t_next
                continue
            target = latest.observed_at + timedelta(hours=1)
            actual = level_at(series, target)
            if actual is None:
                t = t_next
                continue
            predicted = latest.level + rate.rate_m_per_h
            change = actual - latest.level
            tol = max(FORECAST_ABS_TOL_M, FORECAST_REL_TOL * abs(change))
            fc_n += 1
            fc_ok += abs(predicted - actual) <= tol
            persist_ok += abs(latest.level - actual) <= tol
            bucket = "rose<0.05m" if change < 0.05 else "rose0.05-0.2m" if change < 0.2 else "rose>=0.2m"
            b = buckets[bucket]
            b[0] += 1
            b[1] += abs(predicted - actual) <= tol
            b[2] += abs(latest.level - actual) <= tol
            if abs(predicted - actual) > tol and len(examples["forecast"]) < args.show:
                examples["forecast"].append(
                    f"{sid} @ {latest.observed_at:%H:%M} level {latest.level} rate {rate.rate_m_per_h} "
                    f"predicted {predicted:.3f} actual {actual:.3f}"
                )
            t = t_next
    report["forecast_1h_rising"] = {
        "pct": pct(fc_ok, fc_n),
        "n": fc_n,
        "persistence_baseline_pct": pct(persist_ok, fc_n),
        "by_actual_rise": {
            k: {"n": v[0], "pct": pct(v[1], v[0]), "persistence_pct": pct(v[2], v[0])}
            for k, v in sorted(buckets.items())
        },
    }

    report["data"] = {
        "idz65910_files": len(wl_files),
        "idz65911_files": len(glob.glob(os.path.join(d, "IDZ65911_*.hcs"))),
        "map_snapshots": len({s for s, _, _ in snapshots}),
        "gauges_with_history": len(timeline),
    }
    print(json.dumps(report, indent=2))
    for name, lines in examples.items():
        print(f"\n-- {name} mismatches --")
        for line in lines:
            print("  " + line)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"report": report, "examples": examples}, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
