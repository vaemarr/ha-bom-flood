"""Pure data handling for BOM gauge data: parsing, screening, rates, status.

Nothing in this module imports Home Assistant. Everything that decides what a
user sees during a flood lives here, so it can be unit tested and replayed
against recorded BOM files without a running HA instance.

Times are always timezone-aware UTC datetimes. Callers pass `now` explicitly.
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any
from collections.abc import Iterable

from .const import (
    DATUM_SHIFT_SAMPLES,
    EARLIEST_BAND_MIDPOINTS_H,
    EARLIEST_FACTORS,
    HEADS_UP_FRACTION,
    HEADS_UP_HOURS,
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
    SPIKE_HOLD_M,
    RAINFALL_MAX_AGE_HOURS,
    HISTORY_HOURS,
    RATE_HISTORY_HOURS,
    RATE_MIN_SPAN_HOURS,
    RATE_WINDOW_HOURS,
    RATE_WINDOW_MAX_HOURS,
    STALE_ARRIVAL_MIN_SAMPLES,
    STALE_ARRIVAL_MULTIPLIER,
    STALE_ARRIVAL_SAMPLES,
    STALE_CADENCE_MULTIPLIER,
    STALE_FALLBACK_HOURS,
    STALE_MAX_HOURS,
    STALE_MIN_HOURS,
    TREND_FALLING,
    TREND_RISING,
    TREND_STEADY,
    TIDE_BAND_HOURS,
    TIDE_BAND_MARGIN_M,
    TIDE_CONFIRM_HOURS,
    TIDE_MIN_FIT,
    TIDE_MIN_RANGE_M,
    TIDE_MIN_SPAN_HOURS,
    TIDE_OFF_FIT,
    TIDE_OFF_RANGE_M,
    TIDE_PERIOD_HOURS,
    TREND_FALLING_THRESHOLD_M_PER_H,
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
    if rate < -TREND_FALLING_THRESHOLD_M_PER_H:
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


def heads_up_hours(level: float | None, rate: float | None, minor: float | None) -> float | None:
    """Straight-line hours to minor flood, if this reading alone qualifies for a heads-up."""
    if level is None or minor is None or minor <= 0 or level < HEADS_UP_FRACTION * minor:
        return None
    hours = hours_to_level(level, rate, minor)
    return hours if hours is not None and hours <= HEADS_UP_HOURS else None


def earliest_hours(projected: float | None) -> float | None:
    """Earliest likely hours to a level, from the straight-line projection.

    Rivers usually speed up before a flood level, so a straight line tends to
    be late. This scales it by a factor calibrated on historical floods such
    that the real time was no earlier than this in about 94% of cases.
    """
    if projected is None:
        return None
    x = math.log(max(projected, 1e-3))
    mids = [math.log(m) for m in EARLIEST_BAND_MIDPOINTS_H]
    if x <= mids[0]:
        factor = EARLIEST_FACTORS[0]
    elif x >= mids[-1]:
        factor = EARLIEST_FACTORS[-1]
    else:
        i = next(k for k in range(len(mids) - 1) if mids[k] <= x <= mids[k + 1])
        f = (x - mids[i]) / (mids[i + 1] - mids[i])
        factor = EARLIEST_FACTORS[i] + f * (EARLIEST_FACTORS[i + 1] - EARLIEST_FACTORS[i])
    return round(max(projected * factor, 0.1), 1)


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


def is_jump(previous: Observation, current: Observation, slope: float = 0.0) -> bool:
    """A change big enough that one reading alone should not be believed.

    Measured from where the river was already heading (`slope`, m/h, from the
    step before), so a steady fast rise is not held back reading after reading.
    """
    elapsed = max(_hours(previous.observed_at, current.observed_at), 0.0)
    expected = previous.level + slope * min(elapsed, 1.0)
    return abs(current.level - expected) > SPIKE_HOLD_M * max(1.0, elapsed)


def confirms(before: Observation, held: Observation, after: Observation) -> bool:
    """Does `after` back up the jump from `before` to `held`?

    It does when it stays past the halfway point in the direction of the jump:
    a real rise keeps going or stays up, a bad reading falls straight back.
    """
    jump = held.level - before.level
    return (after.level - before.level) * (1 if jump >= 0 else -1) >= abs(jump) / 2


@dataclass
class IngestResult:
    accepted: bool
    rejected_level: float | None = None
    level_shift: bool = False
    held: bool = False


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
    # A big jump waiting for the next reading to confirm it.
    held: Observation | None = None
    # Age (h) of the newest reading just before each new one was published.
    arrival_ages: list[float] = field(default_factory=list)
    # Tide detection: whether the gauge is tidal now, since when the readings
    # have continuously matched a tide, and the latest tide range.
    tidal: bool = False
    tide_since: datetime | None = None
    tide_range_m: float | None = None

    # -- ingestion ---------------------------------------------------------

    def ingest(self, obs: Observation) -> IngestResult:
        """Screen one reading and, if credible, add it to the history."""
        if not self.history:
            self.history.append(obs)
            return IngestResult(accepted=True)

        last = self.history[-1]
        if obs.observed_at < last.observed_at:
            return self._backfill(obs)

        if self.held is not None and obs.observed_at > last.observed_at:
            result = self._resolve_held(last, obs)
            if result is not None:
                return result
            last = self.history[-1]

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
            if is_jump(last, obs, self._last_slope()):
                self.held = obs
                return IngestResult(accepted=False, held=True)
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

    def _last_slope(self) -> float:
        """Recent rate (m/h): latest reading against one at least MIN_STEP_HOURS older.

        Gauges logging every minute or two jitter by tenths of a metre; a slope
        between two such readings is noise, not where the river is heading.
        """
        if len(self.history) < 2:
            return 0.0
        b = self.history[-1]
        a = next(
            (o for o in reversed(self.history[:-1]) if _hours(o.observed_at, b.observed_at) >= MIN_STEP_HOURS),
            None,
        )
        if a is None:
            return 0.0
        return (b.level - a.level) / _hours(a.observed_at, b.observed_at)

    def _resolve_held(self, last: Observation, obs: Observation) -> IngestResult | None:
        """Settle a held jump using the reading after it.

        Returns a result when `obs` was dealt with here, or None to judge `obs`
        normally against the (possibly updated) last good reading.
        """
        held = self.held
        assert held is not None
        if obs.observed_at == held.observed_at:
            self.held = obs  # same reading again, or BOM revised it
            return IngestResult(accepted=False, held=True)
        if obs.observed_at < held.observed_at:
            return None
        self.held = None
        if confirms(last, held, obs) and plausible_step(held, obs):
            self.history.extend([held, obs])
            self.pending.clear()
            self._trim()
            return IngestResult(accepted=True)
        return None  # a one-off: forget it and judge obs against `last`

    def _backfill(self, obs: Observation) -> IngestResult:
        """Insert an older reading only if it fits its neighbours."""
        if any(o.observed_at == obs.observed_at for o in self.history):
            return IngestResult(accepted=True)
        if obs.observed_at < self.history[-1].observed_at - timedelta(hours=HISTORY_HOURS):
            return IngestResult(accepted=False)
        before = [o for o in self.history if o.observed_at < obs.observed_at]
        after = [o for o in self.history if o.observed_at > obs.observed_at]
        if before and not plausible_step(before[-1], obs):
            return IngestResult(accepted=False)
        if after and not plausible_step(obs, after[0]):
            return IngestResult(accepted=False)
        if before and after and is_jump(before[-1], obs) and not confirms(before[-1], obs, after[0]):
            return IngestResult(accepted=False)
        self.history = before + [obs] + after
        return IngestResult(accepted=True)

    def _trim(self) -> None:
        cutoff = self.history[-1].observed_at - timedelta(hours=HISTORY_HOURS)
        self.history = [o for o in self.history if o.observed_at >= cutoff]

    def reset(self) -> None:
        self.history.clear()
        self.pending.clear()
        self.level_shift_at = None
        self.held = None

    def note_arrival(self, previous: Observation, now: datetime) -> None:
        """Record that a newer reading than `previous` was published by `now`."""
        age = _hours(previous.observed_at, now)
        if 0 < age <= 2 * STALE_MAX_HOURS:
            self.arrival_ages = (self.arrival_ages + [round(age, 3)])[-STALE_ARRIVAL_SAMPLES:]

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
            for a, b in zip(self.history, self.history[1:], strict=False)
        ]
        return median(gaps)

    def update_tide(self) -> None:
        """Re-check the readings for a tide (see TIDE_* in const)."""
        latest = self.latest
        if latest is None:
            return
        result = tide_fit(self.history)
        fit, tide_range = result if result is not None else (0.0, 0.0)
        if fit >= TIDE_MIN_FIT and tide_range >= TIDE_MIN_RANGE_M:
            self.tide_range_m = round(tide_range, 2)
            if self.tide_since is None:
                self.tide_since = latest.observed_at
            if _hours(self.tide_since, latest.observed_at) >= TIDE_CONFIRM_HOURS:
                self.tidal = True
        else:
            self.tide_since = None
            if fit < TIDE_OFF_FIT or tide_range < TIDE_OFF_RANGE_M:
                self.tidal = False
                self.tide_range_m = None

    def is_tidal(self, now: datetime | None = None) -> bool:
        return self.tidal

    def tide_band(self) -> tuple[float, float] | None:
        """Lowest and highest level 3 to 25 h before the latest reading."""
        latest = self.latest
        if latest is None:
            return None
        lo_h, hi_h = TIDE_BAND_HOURS
        levels = [
            o.level for o in self.history
            if lo_h <= _hours(o.observed_at, latest.observed_at) <= hi_h
        ]
        if len(levels) < 4:
            return None
        return min(levels), max(levels)

    def within_tide(self, now: datetime) -> bool:
        """Tidal gauge, level inside yesterday's tide range: movement is the tide."""
        if not self.is_tidal(now):
            return False
        band = self.tide_band()
        latest = self.latest
        if band is None or latest is None:
            return True  # tidal, but not enough history to tell a flood from a tide yet
        return band[0] - TIDE_BAND_MARGIN_M <= latest.level <= band[1] + TIDE_BAND_MARGIN_M

    def stale_after_hours(self) -> float:
        """How old the newest reading may get before it is called out of date."""
        if len(self.arrival_ages) >= STALE_ARRIVAL_MIN_SAMPLES:
            usual = median(self.arrival_ages)
            return min(STALE_MAX_HOURS, max(STALE_MIN_HOURS, STALE_ARRIVAL_MULTIPLIER * usual))
        cadence = self.cadence_hours()
        if cadence is None:
            return STALE_FALLBACK_HOURS
        return min(STALE_MAX_HOURS, max(STALE_MIN_HOURS, STALE_CADENCE_MULTIPLIER * cadence))

    def is_stale(self, now: datetime) -> bool:
        latest = self.latest
        if latest is None:
            return True
        return _hours(latest.observed_at, now) > self.stale_after_hours()

    def rate(self, end: int = -1) -> RateResult | None:
        """Rate of change (m/h) ending at the latest accepted reading.

        `end` picks an earlier reading to end at instead (-2: the one before).

        A robust (Theil-Sen) slope over the last RATE_WINDOW_HOURS, widened up to
        a few reporting intervals for slow gauges, and only once the readings
        span RATE_MIN_SPAN_HOURS: a rate from readings minutes apart is mostly
        gauge noise, and this number drives flood projections.
        """
        if len(self.history) < -end:
            return None
        history = self.history[: len(self.history) + end + 1]
        latest = history[-1]
        cadence = self.cadence_hours()
        max_window = RATE_WINDOW_MAX_HOURS
        if cadence is not None:
            max_window = min(RATE_HISTORY_HOURS, max(RATE_WINDOW_MAX_HOURS, 2.5 * cadence))

        window = RATE_WINDOW_HOURS
        while True:
            points = [
                o for o in history
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
            "arrival_ages": list(self.arrival_ages),
            "tidal": self.tidal,
            "tide_since": self.tide_since.isoformat() if self.tide_since else None,
            "tide_range_m": self.tide_range_m,
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
        ages = data.get("arrival_ages")
        if isinstance(ages, list):
            tracker.arrival_ages = [
                float(a) for a in ages
                if isinstance(a, (int, float)) and not isinstance(a, bool) and 0 < a <= 2 * STALE_MAX_HOURS
            ][-STALE_ARRIVAL_SAMPLES:]
        tracker.tidal = data.get("tidal") is True
        since = data.get("tide_since")
        tracker.tide_since = parse_utc(since) if isinstance(since, str) else None
        tide_range = data.get("tide_range_m")
        if isinstance(tide_range, (int, float)) and not isinstance(tide_range, bool):
            tracker.tide_range_m = float(tide_range)
        datum = data.get("datum")
        tracker.datum = datum if isinstance(datum, str) else None
        return tracker


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve a small linear system by Gaussian elimination with pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def _lstsq_sse(rows: list[list[float]], y: list[float]) -> tuple[float, list[float]] | None:
    """Least-squares fit; returns (sum of squared errors, coefficients)."""
    k = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    aty = [sum(r[i] * v for r, v in zip(rows, y, strict=True)) for i in range(k)]
    coef = _solve(ata, aty)
    if coef is None:
        return None
    sse = sum((v - sum(c * x for c, x in zip(coef, r, strict=True))) ** 2 for r, v in zip(rows, y, strict=True))
    return sse, coef


def tide_fit(history: list[Observation]) -> tuple[float, float] | None:
    """How well a tide explains the readings: (share of variation, range in m).

    Readings are resampled hourly. A smooth curve (quadratic) is fitted alone
    and with 12.42 h and 6.21 h sinusoids added; the share is how much of the
    curve-only error the tide removes. None with under TIDE_MIN_SPAN_HOURS.
    """
    if len(history) < 8:
        return None
    t0, t1 = history[0].observed_at, history[-1].observed_at
    span = _hours(t0, t1)
    if span < TIDE_MIN_SPAN_HOURS:
        return None
    xs = [_hours(t0, o.observed_at) for o in history]
    ys = [o.level for o in history]
    grid, vals, j = [], [], 0
    h = 0.0
    while h <= span:
        while j < len(xs) - 2 and xs[j + 1] < h:
            j += 1
        x0, x1 = xs[j], xs[j + 1]
        v = ys[j] if x1 == x0 else ys[j] + (ys[j + 1] - ys[j]) * (min(max(h, x0), x1) - x0) / (x1 - x0)
        grid.append(h)
        vals.append(v)
        h += 1.0
    w = 2 * math.pi / TIDE_PERIOD_HOURS
    mid = span / 2
    smooth = [[1.0, (g - mid) / mid, ((g - mid) / mid) ** 2] for g in grid]
    tidal = [
        r + [math.sin(w * g), math.cos(w * g), math.sin(2 * w * g), math.cos(2 * w * g)]
        for r, g in zip(smooth, grid, strict=True)
    ]
    base = _lstsq_sse(smooth, vals)
    full = _lstsq_sse(tidal, vals)
    if base is None or full is None or base[0] <= 1e-9:
        return None
    coef = full[1]
    return 1 - full[0] / base[0], 2 * math.hypot(coef[3], coef[4])


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
    return drop_spikes(best)


def drop_spikes(series: list[Observation]) -> list[Observation]:
    """Remove single readings that jump away and straight back."""
    out = list(series[:1])
    for i in range(1, len(series)):
        obs = series[i]
        nxt = series[i + 1] if i + 1 < len(series) else None
        if nxt is not None and is_jump(out[-1], obs) and not confirms(out[-1], obs, nxt):
            continue
        out.append(obs)
    return out


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


_QUARTER = timedelta(minutes=15)


def rolling_hour_rainfall(
    station_id: str, quarters: dict[datetime, float], now: datetime
) -> tuple[HcsRecord | None, float | None]:
    """Rain in the hour to the latest 15-minute reading, and in that last 15 minutes.

    Needs all four 15-minute readings of that hour; a gap gives None rather than
    an undercount. Too-old readings give None, as for the hourly feed.
    """
    if not quarters:
        return None, None
    end = max(quarters)
    times = [end - _QUARTER * i for i in range(4)]
    if not all(t in quarters for t in times):
        return None, None
    record = HcsRecord(
        station_id, end, round(sum(quarters[t] for t in times), 1), "mm", "", 3600, None
    )
    if fresh_rainfall(record, now) is None:
        return None, None
    return record, quarters[end]


# ---------------------------------------------------------------------------
# BOM flood warnings
# ---------------------------------------------------------------------------

# Flood warning products are ID<state>2xxxx (QLD) or ID<state>3xxxx (others);
# BOM's FTP only holds products that are current, so listing the folder lists
# the active warnings. Weather and marine warnings share these id ranges and
# are told apart by their title.
WARNING_FILE_RE = re.compile(r"^ID[QNVST][23]\d{4}\.txt$")
# A product title starts with the warning type, optionally qualified:
# "Initial Minor Flood Warning for ...", "Final Flood Warning ...", "Flood Watch for ...".
# Anchored, so a sentence like "Refer to Flood Warning products" is not a title.
_TITLE_RE = re.compile(
    r"^(?:(?:Initial|Renewed|Updated|Final|Minor|Moderate|Major|Severe)\s+)*Flood\s+(?:Warning|Watch)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_ISSUED_RE = re.compile(r"^Issued at (.+)$", re.MULTILINE)
_CLASS_RE = re.compile(r"\b(minor|moderate|major)\b", re.IGNORECASE)
# Real warnings say "is likely to exceed the minor flood level (5.00 m) Friday
# morning", "may peak near the minor flood level (4.00 m)", "may reach around
# 3.40 m Thursday evening, with minor flooding" or "is likely to fall below the
# minor flood level (2.00 m) during Thursday evening".
_FORECAST_RE = re.compile(
    r"\b(?:(?:is|are)\s+(?:likely|expected|forecast)\s+to|may|could|will)\s+"
    r"(?P<verb>reach|peak|exceed|remain|rise\s+to|rise\s+above|ease\s+to|fall\s+to|fall\s+below|drop\s+below)\s+"
    r"(?P<qual>(?:around|near|above|below|at|about)\s+)?"
    r"(?:the\s+(?P<threshold>minor|moderate|major)\s+flood\s+level\s*\(\s*)?"
    r"(?P<level>-?\d+(?:\.\d+)?)\s*(?:m\b|metres?)\)?"
    r"(?P<rest>[^.]*)",
    re.IGNORECASE,
)
_WITH_CLASS_RE = re.compile(r"\bwith\s+(minor|moderate|major)\s+flooding\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^[A-Z][^.:]{2,80}:$")
# A sentence that carries on about the gauge named just before it.
_CONTINUES_RE = re.compile(r"^(?:The|This)\s+(?:river|creek|brook)\b|^Further\s+(?:rises|falls)\b|^It\b", re.IGNORECASE)
_OTHER_GAUGE_RE = re.compile(r"\b(?:River|Creek|Brook)\s+at\s+[A-Z]")
_STOP_SECTIONS = re.compile(r"^(Latest River Heights|River Heights|Next issue|Warning Information)\b", re.IGNORECASE)


@dataclass(frozen=True)
class FloodWarning:
    product_id: str
    title: str
    issued: str | None
    severity: str | None  # minor / moderate / major from the title, if stated
    body: str             # narrative text, without the river height table

    @property
    def final(self) -> bool:
        """BOM's last warning of an event: flooding has ended or is ending."""
        return self.title.lower().startswith("final ")

    @property
    def watch(self) -> bool:
        """A Flood Watch: flooding is possible in coming days, not yet a warning."""
        return re.match(r"^flood\s+watch\b", self.title, re.IGNORECASE) is not None

    @property
    def url(self) -> str:
        return f"https://www.bom.gov.au/cgi-bin/wrap_fwo.pl?{self.product_id}.html"


@dataclass(frozen=True)
class GaugeForecast:
    sentences: tuple[str, ...]
    level_m: float | None
    flood_class: str | None
    when: str | None
    direction: str | None = None  # rise / peak / remain / fall


def parse_flood_warning(product_id: str, text: str) -> FloodWarning | None:
    """Parse a BOM flood warning text product; None if it is not a flood warning."""
    head = "\n".join(text.splitlines()[:12])
    title_match = _TITLE_RE.search(head)
    if title_match is None or "marine" in title_match.group(0).lower():
        return None
    title = " ".join(title_match.group(0).split())
    issued = _ISSUED_RE.search(text)
    severity = _CLASS_RE.search(title)
    body_lines = []
    for line in text.splitlines():
        if _STOP_SECTIONS.match(line.strip()):
            break
        body_lines.append(line)
    return FloodWarning(
        product_id=product_id,
        title=title,
        issued=issued.group(1).strip() if issued else None,
        severity=severity.group(1).lower() if severity else None,
        body="\n".join(body_lines),
    )


_RIVER_RE = re.compile(r"^(?P<base>.+?)\s+(?P<kind>River|Creek|Brook)\b", re.IGNORECASE)


def _river_pattern(gauge_name: str) -> re.Pattern[str] | None:
    """Match the gauge's river by name, also inside lists like "Logan and Albert Rivers".

    "Albert River at Tamborine" matches "the Logan and Albert Rivers" and
    "Albert River"; "Logan River at ..." matches "Logan, Albert and Coomera Rivers".
    """
    m = _RIVER_RE.match(gauge_name.strip())
    if m is None:
        return None
    base = r"\s+".join(re.escape(w) for w in m.group("base").split())
    kind = m.group("kind")
    others = r"(?:\s*(?:,|\band\b|&)\s*(?:the\s+)?[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)*"
    return re.compile(rf"\b{base}{others}\s+{kind}s?\b", re.IGNORECASE)


def warning_names_river(warning: FloodWarning, gauge_name: str) -> bool:
    """A flood warning whose title names this gauge's river.

    Only the title: river names repeat across a state (several Cedar Creeks in
    Queensland), and the title is where BOM says which rivers it covers.
    """
    pattern = _river_pattern(gauge_name)
    return pattern is not None and pattern.search(warning.title) is not None


def watch_covers_river(warning: FloodWarning, gauge_name: str) -> bool:
    """A Flood Watch that lists this gauge's river among the catchments at risk."""
    pattern = _river_pattern(gauge_name)
    return pattern is not None and pattern.search(warning.title + "\n" + warning.body) is not None


def _location_pattern(gauge_name: str) -> re.Pattern[str]:
    """Match a gauge name allowing BOM's spacing and 'River'/'R' variations."""
    words = [re.escape(w) for w in gauge_name.split()]
    pattern = r"\s+".join(words)
    pattern = pattern.replace(r"River", r"R(?:iver|\.)?")
    return re.compile(pattern, re.IGNORECASE)


def _split_sentences(lines: list[str]) -> list[str]:
    text = " ".join(" ".join(lines).split())
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]


def _paragraph_sentences(body: str) -> list[list[str]]:
    """Body text as paragraphs of sentences; section headings stand alone."""
    paragraphs: list[list[str]] = []
    for block in re.split(r"\n\s*\n", body):
        sentences: list[str] = []
        lines: list[str] = []
        for line in block.splitlines():
            if _HEADING_RE.match(line.strip()):
                sentences += _split_sentences(lines)
                sentences.append(line.strip())
                lines = []
            else:
                lines.append(line)
        sentences += _split_sentences(lines)
        if sentences:
            paragraphs.append(sentences)
    return paragraphs


_DIRECTION = {"reach": "rise", "exceed": "rise", "rise to": "rise", "rise above": "rise",
              "peak": "peak", "remain": "remain", "ease to": "fall", "fall to": "fall",
              "fall below": "fall", "drop below": "fall"}


def gauge_forecast(warning: FloodWarning, gauge_name: str) -> GaugeForecast | None:
    """What a warning says about one gauge, or None if it does not mention it.

    Sentences naming the gauge are kept, plus sentences straight after them that
    carry on about it ("The river may reach around 5.20 m Friday morning").
    When several forecasts are given, the highest level is reported.
    """
    location = _location_pattern(gauge_name)
    sentences: list[str] = []
    for paragraph in _paragraph_sentences(warning.body):
        ours = False
        for sentence in paragraph:
            if location.search(sentence):
                ours = True
            elif not (ours and _CONTINUES_RE.match(sentence)) or _OTHER_GAUGE_RE.search(sentence):
                ours = False
            if ours:
                sentences.append(sentence)
    if not sentences:
        return None
    best: GaugeForecast | None = None
    for sentence in sentences:
        for match in _FORECAST_RE.finditer(sentence):
            level = safe_float(match.group("level"))
            if level is None:
                continue
            verb = " ".join(match.group("verb").lower().split())
            direction = _DIRECTION.get(verb)
            rest = match.group("rest")
            # Only "with minor flooding" names the class; "just below the minor
            # flood level" must not.
            cls = _WITH_CLASS_RE.search(rest)
            flood_class = cls.group(1).lower() if cls else None
            threshold = match.group("threshold")
            qual = (match.group("qual") or "").strip().lower()
            above = (direction == "rise" and qual in ("", "above")) or (direction in ("peak", "remain") and qual == "above")
            if flood_class is None and threshold and above:
                flood_class = threshold.lower()  # "exceed the minor flood level (5.00 m)"
            when = re.split(r",|\bwith\b", rest, maxsplit=1)[0].strip() or None
            candidate = GaugeForecast((), level, flood_class, when, direction)
            if best is None or level > (best.level_m or float("-inf")):
                best = candidate
    if best is None:
        return GaugeForecast(tuple(sentences), None, None, None)
    return GaugeForecast(tuple(sentences), best.level_m, best.flood_class, best.when, best.direction)