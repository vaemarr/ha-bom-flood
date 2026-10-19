"""Compare a running Home Assistant's BOM Flood entities with BOM, right now.

Usage:
    HA_URL=http://ha:8123 HA_TOKEN=... python tools/check_live.py REGION [REGION...]

Downloads BOM's current IDZ65910/IDZ65911 files and the regional maps, reads every
bom_flood entity from Home Assistant, and checks for each gauge:

    level      equals BOM's reading at the timestamp HA shows
    time       HA's reading is the newest BOM has published (or one poll behind)
    status     equals BOM's map classification
    trend      equals BOM's map trend (where BOM's map reading is the same one)
    rate       within 0.01 m/h of an independent recomputation from IDZ65911
"""
from __future__ import annotations

import ftplib
import io
import json
import os
import sys
import urllib.request
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from custom_components.bom_flood import analysis as A  # noqa: E402


def ftp_get(name_or_prefix: str) -> str:
    ftp = ftplib.FTP("ftp.bom.gov.au", timeout=60)
    ftp.login()
    ftp.cwd("/anon/gen/fwo/")
    if name_or_prefix.endswith(".html"):
        name = name_or_prefix
    else:
        name = sorted(ftp.nlst(f"{name_or_prefix}_*.hcs"))[-1]
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {name}", buf.write)
    ftp.quit()
    return buf.getvalue().decode("utf-8", errors="replace")


def ha_states() -> list[dict]:
    req = urllib.request.Request(
        os.environ["HA_URL"].rstrip("/") + "/api/states",
        headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}"},
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def main() -> int:
    regions = sys.argv[1:]
    latest = A.parse_hcs(ftp_get("IDZ65910"), "WL")
    dense = A.parse_hcs(ftp_get("IDZ65911"), "WL")
    maps = {}
    for region in regions:
        for m in A.parse_region_map(ftp_get(f"{region}.html")):
            maps[m.station_id] = m

    states = {s["entity_id"]: s for s in ha_states()}
    levels = {
        eid: s for eid, s in states.items()
        if eid.startswith("sensor.") and eid.endswith("_water_level") and "station_id" in s["attributes"]
    }
    results = []
    for eid, s in sorted(levels.items()):
        base = eid[len("sensor."):-len("_water_level")]
        sid = s["attributes"]["station_id"]
        observed = A.parse_utc(s["attributes"].get("observed_at"))
        level = float(s["state"]) if s["state"] not in ("unknown", "unavailable") else None
        rows = {r.observed_at: r.value for r in dense.records.get(sid, [])}
        newest = latest.latest(sid)
        if newest:
            rows[newest.observed_at] = newest.value
        checks = {}
        bom_value = rows.get(observed)
        checks["level"] = bom_value is not None and level is not None and abs(bom_value - level) < 0.0005
        published = sorted(t for t in rows if t > observed) if observed else []
        checks["time"] = observed is not None and len(published) <= 1
        m = maps.get(sid)
        status = states.get(f"sensor.{base}_flood_status", {}).get("state")
        checks["status"] = m is not None and status == m.bom_status
        trend = states.get(f"sensor.{base}_trend", {}).get("state")
        rate_state = states.get(f"sensor.{base}_rate_of_rise", {}).get("state")
        # Independent rate from BOM's readings up to the one HA shows.
        tracker = A.GaugeTracker()
        for t in sorted(rows):
            if observed and t <= observed and rows[t] is not None:
                tracker.ingest(A.Observation(t, rows[t]))
        ref = tracker.rate()
        if rate_state not in (None, "unknown", "unavailable") and ref is not None:
            checks["rate"] = abs(float(rate_state) - ref.rate_m_per_h) <= 0.01
        if m is not None and m.bom_trend and trend not in (None, "unknown", "unavailable"):
            checks["trend"] = trend == m.bom_trend
        results.append((eid, sid, level, observed, status, trend, rate_state, m, ref, checks))

    total = passed = 0
    for eid, sid, level, observed, status, trend, rate, m, ref, checks in results:
        total += len(checks)
        passed += sum(checks.values())
        flag = "OK " if all(checks.values()) else "DIFF"
        print(f"{flag} {eid} ({sid})")
        print(f"     HA : {level} m at {observed:%H:%M}Z, {status}, {trend}, {rate} m/h" if observed else f"     HA : {level}")
        if m:
            print(f"     BOM: map {m.bom_level} m at {m.bom_observed_local} local, {m.bom_status}, {m.bom_trend}; "
                  f"recomputed rate {ref.rate_m_per_h if ref else None}")
        print(f"     checks: {checks}")
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
