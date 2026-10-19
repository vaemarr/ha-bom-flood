"""Download historical flood data for tools/backtest_floods.py.

Usage:
    python tools/fetch_flood_history.py MAPS_DIR OUT_DIR

MAPS_DIR holds BOM regional map pages (IDx65xxx.html) for gauge names, positions
and flood levels. Each classified gauge is matched to the nearest BOM Water Data
Online station within 400 m; pairs whose current levels differ by more than
0.15 m (different datum) are skipped. For the rest, the quality-controlled
level series is downloaded for each event in EVENTS.

Be gentle with BOM's service: requests are sequential, with a pause between.
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from custom_components.bom_flood import analysis as A  # noqa: E402

BASE = (
    "https://www.bom.gov.au/waterdata/services?service=kisters&type=queryServices"
    "&datasource=0&format=json&"
)
EVENTS = {
    "2011-01": ("2011-01-07", "2011-01-17"),
    "2013-01": ("2013-01-23", "2013-02-02"),
    "2017-03": ("2017-03-27", "2017-04-05"),
    "2021-03": ("2021-03-17", "2021-03-28"),
    "2022-02": ("2022-02-21", "2022-03-10"),
    "2022-07": ("2022-06-30", "2022-07-11"),
}
MATCH_KM = 0.4
SAME_DATUM_M = 0.15


def get(params: dict) -> object:
    url = BASE + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return json.load(resp)
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    return None


def main() -> int:
    maps_dir, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    gauges = {}
    for path in glob.glob(os.path.join(maps_dir, "*.html")):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for m in A.parse_region_map(fh.read()):
                if any(v is not None for v in (m.minor_m, m.moderate_m, m.major_m)):
                    gauges[m.station_id] = m
    stations = get({
        "request": "getStationList",
        "parametertype_name": "Water Course Level",
        "returnfields": "station_name,station_no,station_latitude,station_longitude",
    })[1:]
    stations = [(n, no, float(la), float(lo)) for n, no, la, lo in stations if la and lo]

    meta_path = os.path.join(out, "meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    for m in gauges.values():
        if m.station_id in meta:
            continue
        best = None
        for name, no, lat, lon in stations:
            km = math.hypot((lat - m.lat) * 111, (lon - m.lon) * 111 * math.cos(math.radians(m.lat)))
            if km < MATCH_KM and (best is None or km < best[0]):
                best = (km, name, no)
        if best is None:
            continue
        record = {"fwo": m.station_id, "name": m.name, "wdo": best[2], "wdo_name": best[1],
                  "km": round(best[0], 3), "minor": m.minor_m, "moderate": m.moderate_m,
                  "major": m.major_m, "level_now": m.bom_level, "lat": m.lat}
        try:
            series = get({"request": "getTimeseriesList", "station_no": best[2],
                          "returnfields": "ts_id,ts_name,parametertype_name"})
            ids = [r[0] for r in series[1:] if r[1] == "DMQaQc.Merged.AsStored.1" and r[2] == "Water Course Level"]
            if not ids:
                meta[m.station_id] = {**record, "ts_id": None}
                continue
            recent = get({"request": "getTimeseriesValues", "ts_id": ids[0], "period": "P3D",
                          "returnfields": "Timestamp,Value"})[0]["data"]
            last = next((v for _, v in reversed(recent) if v is not None), None)
            meta[m.station_id] = {**record, "ts_id": ids[0], "wdo_last": last}
            if last is None or m.bom_level is None or abs(last - m.bom_level) > SAME_DATUM_M:
                continue
            for event, (start, end) in EVENTS.items():
                path = os.path.join(out, f"{m.station_id}_{event}.json")
                if os.path.exists(path):
                    continue
                values = get({"request": "getTimeseriesValues", "ts_id": ids[0],
                              "from": f"{start}T00:00:00+10:00", "to": f"{end}T00:00:00+10:00",
                              "returnfields": "Timestamp,Value,Quality Code"})[0]["data"]
                with open(path, "w") as fh:
                    json.dump(values, fh)
                time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            print(m.station_id, "error", exc, flush=True)
        with open(meta_path, "w") as fh:
            json.dump(meta, fh, indent=1)
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
