"""Fetches BOM data and turns it into per-gauge readings.

The decisions about what a reading means — screening, rates, status — live in
analysis.py. This module handles I/O: FTP with retries, the hourly side feeds,
persistence, and what to show when BOM cannot be reached.
"""
from __future__ import annotations

import asyncio
import ftplib
import io
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import analysis as A
from .const import (
    CONF_CUSTOM_THRESHOLDS,
    DATUM_AGREEMENT_M,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_STATION_META,
    CONF_STATIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FETCH_ATTEMPTS,
    FETCH_RETRY_DELAYS_S,
    FTP_HOST,
    FTP_PATH,
    FTP_TIMEOUT_S,
    HCS_RAINFALL_15MIN_PREFIX,
    HCS_RAINFALL_HOURLY_PREFIX,
    HCS_WATER_LEVEL_DENSE_PREFIX,
    HCS_WATER_LEVEL_PREFIX,
    KEEP_SERVING_HOURS,
    LEVEL_KEYS,
    MAP_REFRESH_HOURS,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    THRESHOLD_KEYS,
    THRESHOLDS_SOURCE_BOM,
    THRESHOLDS_SOURCE_USER,
    UPDATE_TIMEOUT_S,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Station metadata
# ---------------------------------------------------------------------------


@dataclass
class StationMeta:
    station_id: str
    name: str
    lat: float
    lon: float
    minor_m: float | None
    moderate_m: float | None
    major_m: float | None
    thresholds_source: str = THRESHOLDS_SOURCE_BOM

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "minor_m": self.minor_m,
            "moderate_m": self.moderate_m,
            "major_m": self.major_m,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StationMeta:
        return cls(
            station_id=d["station_id"],
            name=d.get("name") or d["station_id"],
            lat=d.get("lat", 0.0),
            lon=d.get("lon", 0.0),
            minor_m=d.get("minor_m"),
            moderate_m=d.get("moderate_m"),
            major_m=d.get("major_m"),
        )

    @classmethod
    def from_map(cls, m: A.MapStation) -> StationMeta:
        return cls(
            station_id=m.station_id,
            name=m.name,
            lat=m.lat,
            lon=m.lon,
            minor_m=m.minor_m,
            moderate_m=m.moderate_m,
            major_m=m.major_m,
        )

    def thresholds(self) -> dict[str, float | None]:
        return {"minor": self.minor_m, "moderate": self.moderate_m, "major": self.major_m}


def get_custom_thresholds(entry: ConfigEntry) -> dict[str, dict[str, float]]:
    """User-supplied thresholds, preferring options over data."""
    return entry.options.get(
        CONF_CUSTOM_THRESHOLDS, entry.data.get(CONF_CUSTOM_THRESHOLDS, {})
    )


def apply_overrides(
    meta: StationMeta, override: dict[str, float] | None
) -> StationMeta:
    """User levels win over BOM's, per level, so a partial override keeps the rest."""
    applied = {
        key: override[key]
        for key in THRESHOLD_KEYS
        if override and override.get(key) is not None
    }
    if applied:
        return replace(meta, thresholds_source=THRESHOLDS_SOURCE_USER, **applied)
    return replace(meta, thresholds_source=THRESHOLDS_SOURCE_BOM)


def build_station_meta(entry: ConfigEntry) -> dict[str, StationMeta]:
    """Station metadata as stored at setup, with user thresholds applied."""
    overrides = get_custom_thresholds(entry)
    metas: dict[str, StationMeta] = {}
    for station_id, stored in entry.data.get(CONF_STATION_META, {}).items():
        if not stored:
            continue
        metas[station_id] = apply_overrides(
            StationMeta.from_dict({"station_id": station_id, **stored}),
            overrides.get(station_id),
        )
    return metas


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


@dataclass
class StationReading:
    """Everything the entities show for one gauge, as of one poll."""

    station_id: str
    name: str
    level: float | None
    observed_at: datetime | None
    datum: str | None
    quality: int | None
    flood_status: str | None
    rate_m_per_h: float | None
    rate_window_h: float | None
    stale_after_min: float
    thresholds: dict[str, float | None]
    thresholds_source: str
    thresholds_checked_at: datetime | None
    lat: float | None = None
    lon: float | None = None
    rejected_reading: float | None = None
    level_shift_at: datetime | None = None
    rainfall_mm: float | None = None
    rainfall_observed_at: datetime | None = None
    rainfall_15min_mm: float | None = None
    rainfall_source: str | None = None  # "15-minute" or "hourly"
    bom_status: str | None = None
    bom_trend: str | None = None
    feed_ok: bool = True
    warning: A.FloodWarning | None = None
    forecast: A.GaugeForecast | None = None
    warnings_ok: bool | None = None

    # Time-dependent values are derived on read, so a reading displayed long
    # after it was fetched (a hung or failing feed) still ages honestly.

    def age_minutes(self, now: datetime) -> float | None:
        if self.observed_at is None:
            return None
        return round((now - self.observed_at).total_seconds() / 60, 1)

    def is_stale(self, now: datetime) -> bool:
        age = self.age_minutes(now)
        return age is None or age > self.stale_after_min

    def rate(self, now: datetime) -> float | None:
        """The rate, unless the reading is too old to say what the river is doing."""
        if self.is_stale(now):
            return None
        return self.rate_m_per_h

    def trend(self, now: datetime) -> str | None:
        return A.trend_from_rate(self.rate(now))

    def hours_to(self, now: datetime) -> dict[str, float | None]:
        rate = self.rate(now)
        return {
            name: A.hours_to_level(self.level, rate, self.thresholds.get(name))
            for name in LEVEL_KEYS
        }

    def earliest_to(self, now: datetime) -> dict[str, float | None]:
        """Earliest likely hours to each level (calibrated, conservative)."""
        return {name: A.earliest_hours(h) for name, h in self.hours_to(now).items()}

    def next_level(self, now: datetime) -> tuple[str | None, float | None]:
        """The lowest flood level not yet reached, and the earliest likely hours to it."""
        if self.level is None:
            return None, None
        earliest = self.earliest_to(now)
        for name in LEVEL_KEYS:
            threshold = self.thresholds.get(name)
            if threshold is not None and self.level < threshold:
                return name, earliest[name]
        return None, None


# ---------------------------------------------------------------------------
# FTP
# ---------------------------------------------------------------------------


def _with_retries(func, *args):
    """Call a blocking FTP function, retrying transient failures."""
    last_exc: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            return func(*args)
        except (OSError, EOFError, ftplib.Error, A.HcsFormatError) as exc:
            last_exc = exc
            if attempt < FETCH_ATTEMPTS - 1:
                time.sleep(FETCH_RETRY_DELAYS_S[min(attempt, len(FETCH_RETRY_DELAYS_S) - 1)])
    assert last_exc is not None
    raise last_exc


def _ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP(FTP_HOST, timeout=FTP_TIMEOUT_S)
    ftp.login()
    ftp.cwd(FTP_PATH)
    return ftp


def _download(ftp: ftplib.FTP, filename: str) -> str:
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {filename}", buf.write)
    return buf.getvalue().decode("utf-8", errors="replace")


def _latest_name(prefix: str) -> str:
    ftp = _ftp_connect()
    try:
        names = ftp.nlst(f"{prefix}_*.hcs")
    finally:
        _quit(ftp)
    if not names:
        raise ftplib.error_perm(f"No {prefix} files on BOM FTP")
    return sorted(names)[-1]


def _fetch_named(filename: str, sensor_type: str) -> A.HcsFile:
    ftp = _ftp_connect()
    try:
        content = _download(ftp, filename)
    finally:
        _quit(ftp)
    return A.parse_hcs(content, sensor_type)


def _fetch_html_map(product_code: str) -> str:
    """Download a BOM regional flood map page."""
    ftp = _ftp_connect()
    try:
        return _download(ftp, f"{product_code}.html")
    finally:
        _quit(ftp)


def _fetch_flood_warnings() -> list[A.FloodWarning]:
    """Every current BOM flood warning or watch, nationally."""
    ftp = _ftp_connect()
    try:
        names = [n for n in ftp.nlst("ID*.txt") if A.WARNING_FILE_RE.match(n)]
        warnings = []
        for name in sorted(names):
            warning = A.parse_flood_warning(name[:-4], _download(ftp, name))
            if warning is not None:
                warnings.append(warning)
        return warnings
    finally:
        _quit(ftp)


def fetch_region_map(product_code: str) -> list[A.MapStation]:
    return A.parse_region_map(_with_retries(_fetch_html_map, product_code))


def _quit(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except Exception:  # noqa: BLE001 - closing a dead connection is not an error
        ftp.close()


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@dataclass
class _FeedState:
    last_name: str | None = None
    fetched_at: datetime | None = None


@dataclass
class _ThresholdCache:
    by_station: dict[str, StationMeta] = field(default_factory=dict)
    bom_status: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    checked_at: datetime | None = None


class BomFloodCoordinator(DataUpdateCoordinator[dict[str, StationReading]]):
    """Polls BOM and keeps per-gauge history across polls and restarts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=scan_interval),
        )
        self._entry = entry
        self._region: str = entry.data.get(CONF_REGION, "")
        self._station_ids: list[str] = list(entry.data[CONF_STATIONS])
        self._setup_meta: dict[str, StationMeta] = build_station_meta(entry)
        self._overrides = get_custom_thresholds(entry)
        self._trackers: dict[str, A.GaugeTracker] = {}
        self._rejected: dict[str, float] = {}
        self._datum_changed: set[str] = set()
        self._dense = _FeedState()
        self._rain_hourly = _FeedState()
        self._rain_15min = _FeedState()
        # station -> {end of 15-minute period: mm}, last few hours only
        self._quarters: dict[str, dict[datetime, float]] = {}
        self._thresholds = _ThresholdCache()
        self._last_success: datetime | None = None
        self._failures = 0
        self._logged_missing: set[str] = set()
        self._logged_stale: set[str] = set()
        self._logged_unit: set[str] = set()
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._last_rain: dict[str, A.HcsRecord] = {}
        self._warnings: list[A.FloodWarning] = []
        self._warnings_ok: bool | None = None
        self._last_quality: dict[str, int | None] = {}

    @property
    def station_ids(self) -> list[str]:
        return list(self._station_ids)

    def reports_rainfall(self, station_id: str) -> bool:
        """Whether BOM has sent any rainfall for this gauge since startup."""
        return station_id in self._last_rain or bool(self._quarters.get(station_id))

    # -- persistence -------------------------------------------------------

    async def async_load_history(self) -> None:
        """Restore per-gauge history and refreshed thresholds from disk.

        Never fails setup: anything unreadable is dropped and rebuilt from the
        next BOM files, which carry a few hours of readings anyway.
        """
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - corrupt storage must not block setup
            _LOGGER.warning("Stored BOM Flood history is unreadable; starting fresh", exc_info=True)
            return
        if not isinstance(stored, dict):
            return
        stations = stored.get("stations")
        if isinstance(stations, dict):
            for station_id, record in stations.items():
                if station_id in self._station_ids:
                    tracker = A.GaugeTracker.from_dict(record)
                    if tracker.history:
                        self._trackers[station_id] = tracker
        thresholds = stored.get("thresholds")
        checked = A.parse_utc(stored.get("thresholds_checked_at")) if isinstance(
            stored.get("thresholds_checked_at"), str
        ) else None
        if isinstance(thresholds, dict) and checked is not None:
            for station_id, data in thresholds.items():
                try:
                    self._thresholds.by_station[station_id] = StationMeta.from_dict(
                        {"station_id": station_id, **data}
                    )
                except (KeyError, TypeError):
                    continue
            self._thresholds.checked_at = checked
        _LOGGER.debug("Restored history for %d gauge(s)", len(self._trackers))

    def _snapshot(self) -> dict[str, Any]:
        return {
            "stations": {sid: t.to_dict() for sid, t in self._trackers.items()},
            "thresholds": {
                sid: m.to_dict() for sid, m in self._thresholds.by_station.items()
                if sid in self._station_ids
            },
            "thresholds_checked_at": (
                self._thresholds.checked_at.isoformat() if self._thresholds.checked_at else None
            ),
        }

    async def async_flush(self) -> None:
        """Write history now; used on unload so a reload never reads stale data."""
        await self._store.async_save(self._snapshot())

    # -- metadata ----------------------------------------------------------

    def station_meta(self, station_id: str) -> StationMeta | None:
        """Current metadata: latest BOM map values, then user overrides on top."""
        base = self._thresholds.by_station.get(station_id) or self._setup_meta.get(station_id)
        if base is None:
            return None
        return apply_overrides(base, self._overrides.get(station_id))

    def _map_due(self, now: datetime) -> bool:
        if self._datum_changed:
            return True
        checked = self._thresholds.checked_at
        return checked is None or now - checked >= timedelta(hours=MAP_REFRESH_HOURS)

    async def _refresh_map(self, now: datetime) -> None:
        """Pick up revised flood levels and BOM's own status for cross-checking."""
        if not self._region:
            return
        try:
            stations = await self.hass.async_add_executor_job(fetch_region_map, self._region)
        except Exception as exc:  # noqa: BLE001 - map is supplementary
            _LOGGER.debug("Could not refresh BOM map %s: %s", self._region, exc)
            return
        if not stations:
            _LOGGER.warning(
                "BOM map %s returned no gauges; keeping the flood levels already held",
                self._region,
            )
            return
        by_id = {s.station_id: s for s in stations}
        for station_id in self._station_ids:
            m = by_id.get(station_id)
            if m is None:
                continue
            new = StationMeta.from_map(m)
            old = self._thresholds.by_station.get(station_id) or self._setup_meta.get(station_id)
            if old is not None and old.thresholds() != new.thresholds():
                _LOGGER.warning(
                    "BOM changed the flood levels for %s: %s -> %s",
                    m.name, old.thresholds(), new.thresholds(),
                )
            self._thresholds.by_station[station_id] = new
            self._thresholds.bom_status[station_id] = (m.bom_status, m.bom_trend)
            if station_id in self._datum_changed and self._map_agrees(station_id, m):
                self._datum_changed.discard(station_id)
        self._thresholds.checked_at = now

    def _map_agrees(self, station_id: str, m: A.MapStation) -> bool:
        """BOM's map shows this gauge on the same datum as the feed.

        After a datum change the flood levels are only trusted again once the
        map's displayed level matches ours, i.e. the map's thresholds are on
        the datum the readings now use.
        """
        tracker = self._trackers.get(station_id)
        latest = tracker.latest if tracker else None
        return (
            latest is not None
            and m.bom_level is not None
            and abs(latest.level - m.bom_level) <= DATUM_AGREEMENT_M
        )

    # -- update ------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, StationReading]:
        now = dt_util.utcnow()
        try:
            async with asyncio.timeout(UPDATE_TIMEOUT_S):
                wl, rain, quarters, dense = await self._fetch_feeds(now)
        except Exception as exc:  # noqa: BLE001 - every failure takes the same path
            return self._handle_failure(now, exc)

        if self._failures:
            _LOGGER.info("BOM water level feed is reachable again")
        self._failures = 0
        self._last_success = now

        if dense is not None:
            self._ingest_dense(dense)
        self._ingest_latest(wl)
        if rain is not None:
            for station_id in self._station_ids:
                record = rain.latest(station_id)
                if record is not None:
                    self._last_rain[station_id] = record
        if quarters is not None:
            self._ingest_quarters(quarters)

        if self._map_due(now):
            await self._refresh_map(now)

        readings = self._build_readings(now, feed_ok=True)
        self._log_gaps(now, readings)
        self._store.async_delay_save(self._snapshot, STORAGE_SAVE_DELAY)
        return readings

    async def _fetch_feeds(
        self, now: datetime
    ) -> tuple[A.HcsFile, A.HcsFile | None, A.HcsFile | None, A.HcsFile | None]:
        run = self.hass.async_add_executor_job
        wl_name = await run(_with_retries, _latest_name, HCS_WATER_LEVEL_PREFIX)
        wl_task = run(_with_retries, _fetch_named, wl_name, "WL")
        # Rain files only change every 15 or 60 minutes; skip unchanged ones.
        rain_task = run(
            self._fetch_optional, HCS_RAINFALL_HOURLY_PREFIX, "RN", self._rain_hourly.last_name
        )
        quarter_task = run(
            self._fetch_optional, HCS_RAINFALL_15MIN_PREFIX, "RN", self._rain_15min.last_name
        )
        dense_task = run(
            self._fetch_optional, HCS_WATER_LEVEL_DENSE_PREFIX, "WL", self._dense.last_name
        )
        warn_task = run(self._fetch_warnings_safe)
        wl, rain_result, quarter_result, dense_result, warnings = await asyncio.gather(
            wl_task, rain_task, quarter_task, dense_task, warn_task
        )
        if warnings is not None:
            self._warnings, self._warnings_ok = warnings, True
        else:
            # Keep the last list rather than silently clearing an active warning.
            self._warnings_ok = False
        rain = quarters = None
        if rain_result:
            self._rain_hourly.last_name, rain = rain_result
        if quarter_result:
            self._rain_15min.last_name, quarters = quarter_result
        dense = None
        if dense_result:
            self._dense.last_name, dense = dense_result
            self._dense.fetched_at = now
        return wl, rain, quarters, dense

    @staticmethod
    def _fetch_warnings_safe() -> list[A.FloodWarning] | None:
        try:
            return _with_retries(_fetch_flood_warnings)
        except Exception as exc:  # noqa: BLE001 - warnings are supplementary
            _LOGGER.debug("BOM flood warnings unavailable: %s", exc)
            return None

    @staticmethod
    def _fetch_optional(
        prefix: str, sensor_type: str, skip_if_name: str | None
    ) -> tuple[str, A.HcsFile] | None:
        """Fetch a supplementary feed; None if unchanged or unavailable."""
        try:
            name = _with_retries(_latest_name, prefix)
            if name == skip_if_name:
                return None
            return name, _with_retries(_fetch_named, name, sensor_type)
        except Exception as exc:  # noqa: BLE001 - supplementary feeds are optional
            _LOGGER.debug("Optional feed %s unavailable: %s", prefix, exc)
            return None

    def _handle_failure(self, now: datetime, exc: Exception) -> dict[str, StationReading]:
        """Keep showing the last readings for a while, ageing visibly.

        Making every entity unavailable on the first FTP hiccup hides levels the
        user still needs and breaks state-based automations. The readings keep
        their BOM timestamps, so age and staleness grow honestly; only after
        KEEP_SERVING_HOURS without BOM do the entities go unavailable.
        """
        self._failures += 1
        if self._failures == 1:
            _LOGGER.warning("BOM water level feed unavailable: %s", exc)
        if self.data and self._last_success and now - self._last_success < timedelta(hours=KEEP_SERVING_HOURS):
            return self._build_readings(now, feed_ok=False)
        raise UpdateFailed(f"BOM water level feed unavailable: {exc}") from exc

    # -- ingestion ---------------------------------------------------------

    def _tracker(self, station_id: str) -> A.GaugeTracker:
        return self._trackers.setdefault(station_id, A.GaugeTracker())

    def _check_datum(self, station_id: str, tracker: A.GaugeTracker, datum: str) -> None:
        if not datum:
            return
        if tracker.datum and tracker.datum != datum:
            _LOGGER.warning(
                "%s changed datum from %s to %s; restarting its history and "
                "re-checking flood levels before classifying it again",
                station_id, tracker.datum, datum,
            )
            tracker.reset()
            self._datum_changed.add(station_id)
        tracker.datum = datum

    def _unit_ok(self, station_id: str, record: A.HcsRecord) -> bool:
        """Water levels must be in metres; anything else is skipped, not converted."""
        if record.unit in ("metres", "m", ""):
            return True
        if station_id not in self._logged_unit:
            _LOGGER.warning(
                "BOM reported %s in unexpected unit %r; ignoring those readings",
                station_id, record.unit,
            )
            self._logged_unit.add(station_id)
        return False

    def _ingest_dense(self, dense: A.HcsFile) -> None:
        """Merge the hourly multi-reading file into each gauge's history."""
        for station_id in self._station_ids:
            records = [
                r for r in dense.records.get(station_id, [])
                if r.value is not None and self._unit_ok(station_id, r)
            ]
            if not records:
                continue
            tracker = self._tracker(station_id)
            self._check_datum(station_id, tracker, records[-1].datum)
            observations = [A.Observation(r.observed_at, r.value) for r in records]
            if not tracker.history:
                # Robust seed: choose the baseline that keeps the most readings.
                tracker.history = A.screen_series(observations)
                continue
            for obs in observations:
                tracker.ingest(obs)

    def _ingest_latest(self, wl: A.HcsFile) -> None:
        for station_id in self._station_ids:
            record = wl.latest(station_id)
            if record is None:
                continue
            self._last_quality[station_id] = record.quality
            if record.value is None or not self._unit_ok(station_id, record):
                continue
            tracker = self._tracker(station_id)
            self._check_datum(station_id, tracker, record.datum)
            result = tracker.ingest(A.Observation(record.observed_at, record.value))
            if result.rejected_level is not None:
                if self._rejected.get(station_id) != result.rejected_level:
                    _LOGGER.warning(
                        "Ignoring implausible reading for %s: %.2f m at %s against %.2f m. "
                        "Holding the last good reading.",
                        station_id, result.rejected_level, record.observed_at.isoformat(),
                        tracker.latest.level if tracker.latest else float("nan"),
                    )
                self._rejected[station_id] = result.rejected_level
            else:
                self._rejected.pop(station_id, None)
            if result.level_shift:
                _LOGGER.warning(
                    "%s reported %d consistent readings near %.2f m, far from its previous "
                    "level; accepting this as a genuine change of level",
                    station_id, len(tracker.history), record.value,
                )

    def _ingest_quarters(self, quarters: A.HcsFile) -> None:
        for station_id in self._station_ids:
            kept = self._quarters.setdefault(station_id, {})
            for record in quarters.records.get(station_id, []):
                if record.value is not None and record.period_s == 900 and record.unit == "mm":
                    kept[record.observed_at] = record.value
            if kept:
                newest = max(kept)
                for when in [w for w in kept if newest - w > timedelta(hours=3)]:
                    del kept[when]

    def _rainfall(
        self, station_id: str, now: datetime
    ) -> tuple[A.HcsRecord | None, float | None, str | None]:
        """Freshest last-hour rain: rolling 15-minute sum, else BOM's hourly total."""
        hourly = A.fresh_rainfall(self._last_rain.get(station_id), now)
        rolling, last_15 = A.rolling_hour_rainfall(
            station_id, self._quarters.get(station_id, {}), now
        )
        if rolling is not None and (hourly is None or rolling.observed_at > hourly.observed_at):
            return rolling, last_15, "15-minute"
        if hourly is not None:
            return hourly, last_15 if rolling is not None else None, "hourly"
        return None, None, None

    def diagnostics(self, now: datetime) -> dict[str, Any]:
        """Internal state for the diagnostics download."""
        return {
            "now": now.isoformat(),
            "last_success": self._last_success.isoformat() if self._last_success else None,
            "consecutive_failures": self._failures,
            "dense_feed": self._dense.last_name,
            "rain_hourly_feed": self._rain_hourly.last_name,
            "rain_15min_feed": self._rain_15min.last_name,
            "thresholds_checked_at": (
                self._thresholds.checked_at.isoformat() if self._thresholds.checked_at else None
            ),
            "datum_changed": sorted(self._datum_changed),
            "gauges": {
                station_id: {
                    "history": [[o.observed_at.isoformat(), o.level] for o in t.history],
                    "pending_rejections": [[o.observed_at.isoformat(), o.level] for o in t.pending],
                    "datum": t.datum,
                    "cadence_h": t.cadence_hours(),
                    "stale_after_h": t.stale_after_hours(),
                    "rate": vars(r) if (r := t.rate()) else None,
                    "level_shift_at": t.level_shift_at.isoformat() if t.level_shift_at else None,
                    "meta": m.to_dict() | {"source": m.thresholds_source} if (m := self.station_meta(station_id)) else None,
                    "bom_map": self._thresholds.bom_status.get(station_id),
                }
                for station_id, t in self._trackers.items()
            },
        }

    # -- output ------------------------------------------------------------

    def _build_readings(self, now: datetime, *, feed_ok: bool) -> dict[str, StationReading]:
        readings: dict[str, StationReading] = {}
        for station_id in self._station_ids:
            tracker = self._trackers.get(station_id)
            latest = tracker.latest if tracker else None
            if tracker is None or latest is None:
                continue
            meta = self.station_meta(station_id)
            thresholds = meta.thresholds() if meta else {"minor": None, "moderate": None, "major": None}
            rate = tracker.rate()
            status = A.derive_flood_status(
                latest.level, thresholds["minor"], thresholds["moderate"], thresholds["major"]
            )
            if station_id in self._datum_changed or meta is None:
                status = None
            rain, rain_15, rain_source = self._rainfall(station_id, now)
            bom_status, bom_trend = self._thresholds.bom_status.get(station_id, (None, None))
            warning, forecast = self._warning_for(meta.name if meta else None)
            readings[station_id] = StationReading(
                station_id=station_id,
                name=meta.name if meta else station_id,
                level=latest.level,
                observed_at=latest.observed_at,
                datum=tracker.datum,
                quality=self._last_quality.get(station_id),
                flood_status=status,
                rate_m_per_h=rate.rate_m_per_h if rate else None,
                rate_window_h=rate.window_hours if rate else None,
                stale_after_min=round(tracker.stale_after_hours() * 60, 1),
                thresholds=thresholds,
                thresholds_source=meta.thresholds_source if meta else THRESHOLDS_SOURCE_BOM,
                thresholds_checked_at=self._thresholds.checked_at,
                lat=meta.lat if meta else None,
                lon=meta.lon if meta else None,
                rejected_reading=self._rejected.get(station_id),
                level_shift_at=tracker.level_shift_at,
                rainfall_mm=rain.value if rain else None,
                rainfall_observed_at=rain.observed_at if rain else None,
                rainfall_15min_mm=rain_15,
                rainfall_source=rain_source,
                bom_status=bom_status,
                bom_trend=bom_trend,
                feed_ok=feed_ok,
                warning=warning,
                forecast=forecast,
                warnings_ok=self._warnings_ok,
            )
        return readings

    def _warning_for(
        self, gauge_name: str | None
    ) -> tuple[A.FloodWarning | None, A.GaugeForecast | None]:
        """The most severe current warning that names this gauge."""
        if not gauge_name:
            return None, None
        rank = {"major": 3, "moderate": 2, "minor": 1}
        best: tuple[int, A.FloodWarning, A.GaugeForecast] | None = None
        for warning in self._warnings:
            forecast = A.gauge_forecast(warning, gauge_name)
            if forecast is None:
                continue
            score = rank.get(warning.severity or "", 0) * 10 + (forecast.level_m is not None)
            if best is None or score > best[0]:
                best = (score, warning, forecast)
        return (best[1], best[2]) if best else (None, None)

    def _log_gaps(self, now: datetime, readings: dict[str, StationReading]) -> None:
        missing = sorted(set(self._station_ids) - set(readings))
        if missing and not readings:
            raise UpdateFailed(
                f"None of the {len(missing)} configured gauge(s) were found in the BOM "
                f"water level files; missing: {', '.join(missing)}"
            )
        if missing and not self._logged_missing.issuperset(missing):
            _LOGGER.warning(
                "No BOM water level data for gauge(s): %s. The remaining %d gauge(s) "
                "are updating normally.",
                ", ".join(missing), len(readings),
            )
        self._logged_missing = set(missing)
        for station_id, reading in readings.items():
            if reading.is_stale(now):
                if station_id not in self._logged_stale:
                    _LOGGER.warning(
                        "BOM has not published a new reading for %s in %.0f minutes; "
                        "the level shown is that old reading, not a current one",
                        reading.name, reading.age_minutes(now) or 0,
                    )
                    self._logged_stale.add(station_id)
            else:
                self._logged_stale.discard(station_id)
