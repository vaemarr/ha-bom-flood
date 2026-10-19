"""Pure data handling for BOM gauge data: parsing, screening, rates, status.

Nothing in this module imports Home Assistant. Everything that decides what a
user sees during a flood lives here, so it can be unit tested and replayed
against recorded BOM files without a running HA instance.

Times are always timezone-aware UTC datetimes. Callers pass `now` explicitly.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

from .const import (
    DATUM_SHIFT_SAMPLES,
    FLOOD_STATUS_BELOW,
    FLOOD_STATUS_MAJOR,
    FLOOD_STATUS_MINOR,
    FLOOD_STATUS_MODERATE,
    FLOOD_STATUS_NO_CLASSIFICATION,
    MAX_FALL_M_PER_H,
    MAX_RISE_FIRST_HOUR_M,
    MAX_RISE_M_PER_H_AFTER,
    MAX_STEP_HOURS,
    MIN_STEP_HOURS,
    PROJECTION_HORIZON_HOURS,
    RAINFALL_MAX_AGE_HOURS,
    RATE_HISTORY_HOURS,
    RATE_MIN_SPAN_HOURS,
    RATE_WINDOW_HOURS,
    RATE_WINDOW_MAX_HOURS,
    STALE_CADENCE_MULTIPLIER,
    STALE_FALLBACK_HOURS,
    STALE_MIN_HOURS,
    TREND_FALLING,
    TREND_RISING,
    TREND_STEADY,
    TREND_THRESHOLD_M_PER_H,
)

# BOM's own wording on its flood maps, mapped to our stable state keys.
BOM_STATUS_TO_KEY = {
    "Below Flood Level": FLOOD_STATUS_BELOW,
    "Minor": FLOOD_STATUS_MINOR,
    "Moderate": FLOOD_STATUS_MODERATE,
    "Major": FLOOD_STATUS_MAJOR,
    "No Classification": FLOOD_STATUS_NO_CLASSIFICATION,
}

LEVEL_NAMES = ("minor", "moderate", "major")


class HcsFormatError(ValueError):
    """The file is not a complete, well-formed BOM HCS file."""


def parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO timestamp. Naive values are rejected, not guessed at."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def safe_float(value: Any) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


# ---------------------------------------------------------------------------
# HCS files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HcsRecord:
    station_id: str
    observed_at: datetime
    value: float | None
    unit: str
    datum: str
    period_s: int | None
    quality: int | None


@dataclass
class HcsFile:
    generated_at: datetime | None
    records: dict[str, list[HcsRecord]]

    def latest(self, station_id: str) -> HcsRecord | None:
        """The newest record for a gauge, by timestamp, never by file order."""
        rows = self.records.get(station_id)
        return rows[-1] if rows else None


_HEADER_RE = re.compile(r"^#\s*HEADER:\s*([^:]+):\s*(.*)$")


def parse_hcs(content: str, sensor_type: str) -> HcsFile:
    """Parse a BOM HCS file, keeping every record of `sensor_type` per station.

    Raises HcsFormatError when the file is empty or shorter than the record
    count its own header declares: a truncated download would otherwise parse
    "successfully" with the gauges past the cut silently missing.
    """
    declared: int | None = None
    generated_at: datetime | None = None
    data_lines: list[str] = []
    for line in content.splitlines():
        if line.startswith("#"):
            match = _HEADER_RE.match(line)
            if match:
                key, value = match.group(1).strip().lower(), match.group(2).strip()
                if key == "number of records":
                    try:
                        declared = int(value)
                    except ValueError:
                        declared = None
                elif key == "file generation date":
                    generated_at = parse_utc(value)
            continue
        if line.strip():
            data_lines.append(line)

    if not data_lines:
        raise HcsFormatError("HCS file contains no records")

    records: dict[str, list[HcsRecord]] = {}
    complete_rows = 0
    for row in csv.reader(data_lines, quotechar='"'):
        if len(row) < 11:
            continue
        complete_rows += 1
        if row[1].strip() != sensor_type:
            continue
        observed_at = parse_utc(row[5])
        if observed_at is None:
            continue
        try:
            quality: int | None = int(row[10].strip())
        except ValueError:
            quality = None
        try:
            period_s: int | None = int(row[9].strip()) if row[9].strip() else None
        except ValueError:
            period_s = None
        station_id = row[4].strip()
        records.setdefault(station_id, []).append(
            HcsRecord(
                station_id=station_id,
                observed_at=observed_at,
                value=safe_float(row[6]),
                unit=row[7].strip(),
                datum=row[8].strip(),
                period_s=period_s,
                quality=quality,
            )
        )

    if declared is not None and complete_rows < declared:
        raise HcsFormatError(
            f"HCS file truncated: header declares {declared} records, found {complete_rows}"
        )

    for rows in records.values():
        rows.sort(key=lambda r: r.observed_at)
    return HcsFile(generated_at=generated_at, records=records)


# ---------------------------------------------------------------------------
# Regional HTML flood maps
# ---------------------------------------------------------------------------

# PopupRiver("Name",'id','lat','lon','level','status','trend','timestamp','minor','mod','major',...)
_POPUP_RE = re.compile(
    r'PopupRiver\("(?P<name>[^"]+)"'
    r",'(?P<station_id>[^']*)'"
    r",'(?P<lat>[^']*)'"
    r",'(?P<lon>[^']*)'"
    r",'(?P<level>[^']*)'"
    r",'(?P<status>[^']*)'"
    r",'(?P<trend>[^']*)'"
    r",'(?P<timestamp>[^']*)'"
    r",'(?P<minor_m>[^']*)'"
    r",'(?P<moderate_m>[^']*)'"
    r",'(?P<major_m>[^']*)'"
)


@dataclass(frozen=True)
class MapStation:
    """One gauge as BOM shows it on a regional flood map."""

    station_id: str
    name: str
    lat: float
    lon: float
    minor_m: float | None
    moderate_m: float | None
    major_m: float | None
    bom_level: float | None
    bom_status: str | None  # our state key, e.g. "minor"
    bom_trend: str | None
    bom_observed_local: str


def parse_region_map(html: str) -> list[MapStation]:
    """Extract every gauge from a BOM regional flood map page."""
    html = html.replace("&quot;", '"')
    seen: set[str] = set()
    stations: list[MapStation] = []
    for match in _POPUP_RE.finditer(html):
        g = match.groupdict()
        lat, lon = safe_float(g["lat"]), safe_float(g["lon"])
        station_id = g["station_id"].strip()
        if lat is None or lon is None or not station_id or station_id in seen:
            continue
        seen.add(station_id)
        trend = g["trend"].strip().lower() or None
        stations.append(
            MapStation(
                station_id=station_id,
                name=" ".join(g["name"].split()),
                lat=lat,
                lon=lon,
                minor_m=safe_float(g["minor_m"]),
                moderate_m=safe_float(g["moderate_m"]),
                major_m=safe_float(g["major_m"]),
                bom_level=safe_float(g["level"]),
                bom_status=BOM_STATUS_TO_KEY.get(g["status"].strip()),
                bom_trend=trend if trend in (TREND_RISING, TREND_FALLING, TREND_STEADY) else None,
                bom_observed_local=g["timestamp"].strip(),
            )
        )
    return stations


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def derive_flood_status(
    level: float | None,
    minor_m: float | None,
    moderate_m: float | None,
    major_m: float | None,
) -> str | None:
    """Classify a level the way BOM does on its maps.

    None means there is nothing to classify. A gauge with any threshold at all is
    "below flood level" until it reaches the lowest one it has — BOM publishes
    some gauges with a moderate and major level but no minor, and shows them as
    Below Flood Level, not unclassified.
    """
    if level is None:
        return None
    if major_m is not None and level >= major_m:
        return FLOOD_STATUS_MAJOR
    if moderate_m is not None and level >= moderate_m:
        return FLOOD_STATUS_MODERATE
    if minor_m is not None and level >= minor_m:
        return FLOOD_STATUS_MINOR
    if minor_m is None and moderate_m is None and major_m is None:
        return FLOOD_STATUS_NO_CLASSIFICATION
    return FLOOD_STATUS_BELOW


def trend_from_rate(rate: float | None) -> str | None:
    """rising / falling / steady, or None when the rate is unknown.

    Unknown is never reported as steady: "steady" reassures, and it must only
    be said when there is a measured rate to back it.
    """
    if rate is None:
        return None
    if rate > TREND_THRESHOLD_M_PER_H:
        return TREND_RISING
    if rate < -TREND_THRESHOLD_M_PER_H:
        return TREND_FALLING
    return TREND_STEADY


def hours_to_level(level: float | None, rate: float | None, threshold: float | None) -> float | None:
    """Hours until `threshold` at the current rate, or None if not meaningful.

    Only projected for a genuinely rising gauge (rate above the trend threshold),
    and only within PROJECTION_HORIZON_HOURS: a 0.002 m/h drift projecting a
    flood in 90 days is noise presented as a forecast.
    """
    if level is None or rate is None or threshold is None:
        return None
    if rate <= TREND_THRESHOLD_M_PER_H or level >= threshold:
        return None
    hours = (threshold - level) / rate
    if hours > PROJECTION_HORIZON_HOURS:
        return None
    return round(hours, 1)


def theil_sen_slope(points: list[tuple[float, float]]) -> float | None:
    """Median of pairwise slopes: robust to a minority of bad samples."""
    slopes = []
    for i in range(len(points)):
        x1, y1 = points[i]
        for j in range(i + 1, len(points)):
            x2, y2 = points[j]
            if x2 != x1:
                slopes.append((y2 - y1) / (x2 - x1))
    return median(slopes) if slopes else None


# ---------------------------------------------------------------------------
# Per-gauge tracking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    level: float


def _hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600


def plausible_step(previous: Observation, current: Observation) -> bool:
    """Could a real river go from `previous` to `current`?

    A physical bound, not outlier statistics: a flash flood genuinely looks like
    a large sudden jump, and filtering those would be dangerous. Rises are
    allowed MAX_RISE_FIRST_HOUR_M in the first hour and MAX_RISE_M_PER_H_AFTER
    per hour beyond; falls, which are physically slower and the usual shape of a
    sensor fault (dropouts, -26 m readings), are allowed MAX_FALL_M_PER_H.

    The elapsed time is floored at MIN_STEP_HOURS so two readings a minute apart
    are not judged as an absurd rate, and capped at MAX_STEP_HOURS so a long gap
    does not make any value at all "plausible".
    """
    elapsed = min(max(_hours(previous.observed_at, current.observed_at), MIN_STEP_HOURS), MAX_STEP_HOURS)
    change = current.level - previous.level
    if change >= 0:
        allowed = MAX_RISE_FIRST_HOUR_M * min(elapsed, 1.0) + MAX_RISE_M_PER_H_AFTER * max(elapsed - 1.0, 0.0)
    else:
        allowed = MAX_FALL_M_PER_H * elapsed
    return abs(change) <= allowed


@dataclass
class IngestResult:
    accepted: bool
    rejected_level: float | None = None
    level_shift: bool = False


@dataclass
class RateResult:
    rate_m_per_h: float
    window_hours: float
    samples: int
    baseline_at: datetime


@dataclass
class GaugeTracker:
    """Everything remembered about one gauge between polls.

    `history` holds accepted observations only, oldest first. `pending` holds
    consecutive rejected readings with distinct timestamps; once enough of them
    agree with each other the gauge is taken to have genuinely shifted.
    """

    history: list[Observation] = field(default_factory=list)
    pending: list[Observation] = field(default_factory=list)
    datum: str | None = None
    level_shift_at: datetime | None = None

    # -- ingestion ---------------------------------------------------------

    def ingest(self, obs: Observation) -> IngestResult:
        """Screen one reading and, if credible, add it to the history."""
        if not self.history:
            self.history.append(obs)
            return IngestResult(accepted=True)

        last = self.history[-1]
        if obs.observed_at < last.observed_at:
            return self._backfill(obs)

        if obs.observed_at == last.observed_at:
            if obs.level == last.level:
                return IngestResult(accepted=True)
            # BOM revised the value for a timestamp we already hold.
            previous = self.history[-2] if len(self.history) > 1 else None
            if previous is None or plausible_step(previous, obs):
                self.history[-1] = obs
                return IngestResult(accepted=True)
            return IngestResult(accepted=False, rejected_level=obs.level)

        if plausible_step(last, obs):
            self.history.append(obs)
            self.pending.clear()
            self._trim()
            return IngestResult(accepted=True)

        # Implausible against the last good reading. Count it towards a level
        # shift only if it is a new BOM observation that agrees with the other
        # rejected ones — the same bad row seen on several polls, or a scatter
        # of unrelated bad values, must never add up to "accepted".
        if self.pending and obs.observed_at <= self.pending[-1].observed_at:
            return IngestResult(accepted=False, rejected_level=obs.level)
        if self.pending and not plausible_step(self.pending[-1], obs):
            self.pending = [obs]
        else:
            self.pending.append(obs)
        if len(self.pending) >= DATUM_SHIFT_SAMPLES:
            self.history = list(self.pending)
            self.pending.clear()
            self.level_shift_at = obs.observed_at
            return IngestResult(accepted=True, level_shift=True)
        return IngestResult(accepted=False, rejected_level=obs.level)

    def _backfill(self, obs: Observation) -> IngestResult:
        """Insert an older reading only if it fits its neighbours."""
        if any(o.observed_at == obs.observed_at for o in self.history):
            return IngestResult(accepted=True)
        if obs.observed_at < self.history[-1].observed_at - timedelta(hours=RATE_HISTORY_HOURS):
            return IngestResult(accepted=False)
        before = [o for o in self.history if o.observed_at < obs.observed_at]
        after = [o for o in self.history if o.observed_at > obs.observed_at]
        if before and not plausible_step(before[-1], obs):
            return IngestResult(accepted=False)
        if after and not plausible_step(obs, after[0]):
            return IngestResult(accepted=False)
        self.history = before + [obs] + after
        return IngestResult(accepted=True)

    def _trim(self) -> None:
        cutoff = self.history[-1].observed_at - timedelta(hours=RATE_HISTORY_HOURS)
        self.history = [o for o in self.history if o.observed_at >= cutoff]

    def reset(self) -> None:
        self.history.clear()
        self.pending.clear()
        self.level_shift_at = None

    # -- derived values ----------------------------------------------------

    @property
    def latest(self) -> Observation | None:
        return self.history[-1] if self.history else None

    def cadence_hours(self) -> float | None:
        """Median gap between observations, or None with fewer than 3."""
        if len(self.history) < 3:
            return None
        gaps = [
            _hours(a.observed_at, b.observed_at)
            for a, b in zip(self.history, self.history[1:])
        ]
        return median(gaps)

    def stale_after_hours(self) -> float:
        cadence = self.cadence_hours()
        if cadence is None:
            return STALE_FALLBACK_HOURS
        return max(STALE_MIN_HOURS, STALE_CADENCE_MULTIPLIER * cadence)

    def is_stale(self, now: datetime) -> bool:
        latest = self.latest
        if latest is None:
            return True
        return _hours(latest.observed_at, now) > self.stale_after_hours()

    def rate(self) -> RateResult | None:
        """Rate of change (m/h) ending at the latest accepted reading.

        A robust (Theil-Sen) slope over the last RATE_WINDOW_HOURS, widened up to
        a few reporting intervals for slow gauges, and only once the readings
        span RATE_MIN_SPAN_HOURS: a rate from readings minutes apart is mostly
        gauge noise, and this number drives flood projections.
        """
        latest = self.latest
        if latest is None:
            return None
        cadence = self.cadence_hours()
        max_window = RATE_WINDOW_MAX_HOURS
        if cadence is not None:
            max_window = min(RATE_HISTORY_HOURS, max(RATE_WINDOW_MAX_HOURS, 2.5 * cadence))

        window = RATE_WINDOW_HOURS
        while True:
            points = [
                o for o in self.history
                if _hours(o.observed_at, latest.observed_at) <= window + 1e-9
            ]
            span = _hours(points[0].observed_at, latest.observed_at)
            if (len(points) >= 3 and span >= RATE_MIN_SPAN_HOURS) or window >= max_window:
                break
            window = min(window * 1.5, max_window)

        if len(points) < 2 or span < RATE_MIN_SPAN_HOURS:
            return None
        t0 = points[0].observed_at
        slope = theil_sen_slope([(_hours(t0, o.observed_at), o.level) for o in points])
        if slope is None:
            return None
        return RateResult(
            rate_m_per_h=round(slope, 3),
            window_hours=round(span, 2),
            samples=len(points),
            baseline_at=t0,
        )

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": [[o.observed_at.isoformat(), o.level] for o in self.history],
            "datum": self.datum,
        }

    @classmethod
    def from_dict(cls, data: Any) -> GaugeTracker:
        """Rebuild from storage, tolerating anything malformed.

        Stored series are re-screened, and the screening baseline is chosen as
        the starting point that keeps the most readings: trusting the oldest
        stored reading would let one bad sample discard every good one after it.
        """
        tracker = cls()
        if not isinstance(data, dict):
            return tracker
        raw: list[Observation] = []
        entries = data.get("observations")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                observed_at = parse_utc(entry[0]) if isinstance(entry[0], str) else None
                level = safe_float(entry[1]) if not isinstance(entry[1], bool) else None
                if observed_at is not None and level is not None:
                    raw.append(Observation(observed_at, level))
        elif data.get("timestamp") is not None:
            # Oldest storage format: one previous observation.
            observed_at = parse_utc(data.get("timestamp")) if isinstance(data.get("timestamp"), str) else None
            level = safe_float(data.get("level"))
            if observed_at is not None and level is not None:
                raw.append(Observation(observed_at, level))
        tracker.history = screen_series(raw)
        datum = data.get("datum")
        tracker.datum = datum if isinstance(datum, str) else None
        return tracker


def screen_series(observations: Iterable[Observation]) -> list[Observation]:
    """Drop implausible readings from a series using a robust starting point."""
    series = sorted({o.observed_at: o for o in observations}.values(), key=lambda o: o.observed_at)
    if len(series) < 2:
        return series
    best: list[Observation] = []
    for start in range(min(len(series), 6)):
        kept = [series[start]]
        for obs in series[start + 1:]:
            if plausible_step(kept[-1], obs):
                kept.append(obs)
        if len(kept) > len(best):
            best = kept
    return best


# ---------------------------------------------------------------------------
# Rainfall
# ---------------------------------------------------------------------------


def fresh_rainfall(record: HcsRecord | None, now: datetime) -> HcsRecord | None:
    """The rainfall record if it is recent enough to call "the last hour"."""
    if record is None or record.value is None:
        return None
    if _hours(record.observed_at, now) > RAINFALL_MAX_AGE_HOURS:
        return None
    return record
