from __future__ import annotations

import asyncio
import csv
import ftplib
import io
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CUSTOM_THRESHOLDS,
    CONF_SCAN_INTERVAL,
    CONF_STATION_META,
    CONF_STATIONS,
    DATUM_SHIFT_SAMPLES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_PLAUSIBLE_RATE_M_PER_H,
    MAX_RATE_AGE_HOURS,
    RATE_HISTORY_HOURS,
    RATE_WINDOW_MAX_HOURS,
    RATE_WINDOW_MIN_HOURS,
    STALE_CADENCE_MULTIPLIER,
    STALE_FALLBACK_HOURS,
    STALE_MIN_HOURS,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    FTP_HOST,
    FTP_PATH,
    FLOOD_STATUS_BELOW,
    FLOOD_STATUS_MAJOR,
    FLOOD_STATUS_MINOR,
    FLOOD_STATUS_MODERATE,
    FLOOD_STATUS_NO_CLASSIFICATION,
    HCS_RAINFALL_HOURLY_PREFIX,
    HCS_WATER_LEVEL_PREFIX,
    THRESHOLD_KEYS,
    THRESHOLDS_SOURCE_BOM,
    THRESHOLDS_SOURCE_USER,
    TREND_FALLING,
    TREND_RISING,
    TREND_STEADY,
    TREND_THRESHOLD_M,
)

_LOGGER = logging.getLogger(__name__)

# BOM HTML encodes station name with &quot; and remaining fields with single quotes:
# PopupRiver("Name",'id','lat','lon','level','status','trend','timestamp','minor','mod','major',...)
_POPUP_RE = re.compile(
    r'PopupRiver\("(?P<name>[^"]+)"'
    r",\'(?P<station_id>[^\']*)\'"
    r",\'(?P<lat>[^\']*)\'"
    r",\'(?P<lon>[^\']*)\'"
    r",\'(?P<level>[^\']*)\'"
    r",\'(?P<status>[^\']*)\'"
    r",\'(?P<trend>[^\']*)\'"
    r",\'(?P<timestamp>[^\']*)\'"
    r",\'(?P<minor_m>[^\']*)\'"
    r",\'(?P<moderate_m>[^\']*)\'"
    r",\'(?P<major_m>[^\']*)\'"
)


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
            name=d["name"],
            lat=d["lat"],
            lon=d["lon"],
            minor_m=d.get("minor_m"),
            moderate_m=d.get("moderate_m"),
            major_m=d.get("major_m"),
        )


def get_custom_thresholds(entry: ConfigEntry) -> dict[str, dict[str, float]]:
    """User-supplied thresholds, preferring options over data."""
    return entry.options.get(
        CONF_CUSTOM_THRESHOLDS, entry.data.get(CONF_CUSTOM_THRESHOLDS, {})
    )


def build_station_meta(entry: ConfigEntry) -> dict[str, StationMeta]:
    """Station metadata with any user-supplied thresholds applied.

    BOM leaves the flood thresholds empty for gauges it does not classify (ALERT
    stations, mostly), which makes a status impossible to derive. A user who knows
    local levels can supply their own; those win over BOM per-level, so a partial
    override still keeps whatever BOM does publish.
    """
    overrides = get_custom_thresholds(entry)
    metas: dict[str, StationMeta] = {}
    for station_id, stored in entry.data.get(CONF_STATION_META, {}).items():
        meta = StationMeta.from_dict(stored)
        override = overrides.get(station_id) or {}
        applied = {
            key: override[key]
            for key in THRESHOLD_KEYS
            if override.get(key) is not None
        }
        if applied:
            meta = replace(meta, thresholds_source=THRESHOLDS_SOURCE_USER, **applied)
        else:
            meta = replace(meta, thresholds_source=THRESHOLDS_SOURCE_BOM)
        metas[station_id] = meta
    return metas


@dataclass
class StationReading:
    station_id: str
    level: float | None
    timestamp: str
    quality: int
    trend: str
    flood_status: str | None
    rate_of_rise_m_per_hr: float | None
    rainfall_mm_1h: float | None
    rate_calculated_at: str | None = None
    reading_age_min: float | None = None
    stale_after_min: float | None = None
    stale: bool = False
    rejected_reading: float | None = None


def _ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP(FTP_HOST, timeout=30)
    ftp.login()
    return ftp


def _fetch_latest_hcs_file(prefix: str) -> str:
    """List BOM FTP, download the newest file matching prefix_*.hcs, return contents."""
    ftp = _ftp_connect()
    try:
        ftp.cwd(FTP_PATH)
        filenames = ftp.nlst(f"{prefix}_*.hcs")
        if not filenames:
            raise UpdateFailed(f"No {prefix} HCS files found on BOM FTP")
        latest = sorted(filenames)[-1]
        _LOGGER.debug("Fetching HCS file: %s", latest)
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {latest}", buf.write)
        return buf.getvalue().decode("utf-8", errors="replace")
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def _fetch_latest_hcs() -> str:
    return _fetch_latest_hcs_file(HCS_WATER_LEVEL_PREFIX)


def _fetch_latest_rainfall_hcs() -> str:
    return _fetch_latest_hcs_file(HCS_RAINFALL_HOURLY_PREFIX)


def _fetch_html_map(product_code: str) -> str:
    """Download the BOM HTML flood map for station discovery."""
    ftp = _ftp_connect()
    try:
        ftp.cwd(FTP_PATH)
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {product_code}.html", buf.write)
        return buf.getvalue().decode("utf-8", errors="replace")
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def _safe_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_stations_from_html(html: str) -> list[StationMeta]:
    """Extract station metadata from PopupRiver() calls in BOM HTML map."""
    html = html.replace("&quot;", '"')
    stations: list[StationMeta] = []
    for match in _POPUP_RE.finditer(html):
        g = match.groupdict()
        lat = _safe_float(g["lat"])
        lon = _safe_float(g["lon"])
        if lat is None or lon is None or not g["station_id"]:
            continue
        stations.append(StationMeta(
            station_id=g["station_id"],
            name=g["name"],
            lat=lat,
            lon=lon,
            minor_m=_safe_float(g["minor_m"]),
            moderate_m=_safe_float(g["moderate_m"]),
            major_m=_safe_float(g["major_m"]),
        ))
    return stations


def _parse_hcs(content: str, sensor_type: str) -> dict[str, tuple[float | None, str, int]]:
    """Parse HCS CSV → dict[station_id, (value, timestamp, quality)] for given sensor type."""
    result: dict[str, tuple[float | None, str, int]] = {}
    reader = csv.reader(
        (line for line in content.splitlines() if not line.startswith("#")),
        quotechar='"',
    )
    for row in reader:
        if len(row) < 10:
            continue
        if row[1].strip().strip('"') != sensor_type:
            continue
        station_id = row[4].strip().strip('"')
        timestamp = row[5].strip().strip('"')
        value = _safe_float(row[6])
        try:
            quality = int(row[10].strip().strip('"'))
        except (ValueError, IndexError):
            quality = 0
        result[station_id] = (value, timestamp, quality)
    return result


def _derive_flood_status(level: float | None, meta: StationMeta | None) -> str | None:
    """Classify a level against BOM's thresholds.

    None (state 'unknown') means we cannot classify: no reading, or no stored
    metadata. NO_CLASSIFICATION means we can, and BOM sets no thresholds here.
    """
    if level is None or meta is None:
        return None
    if meta.major_m is not None and level >= meta.major_m:
        return FLOOD_STATUS_MAJOR
    if meta.moderate_m is not None and level >= meta.moderate_m:
        return FLOOD_STATUS_MODERATE
    if meta.minor_m is not None and level >= meta.minor_m:
        return FLOOD_STATUS_MINOR
    if meta.minor_m is not None:
        return FLOOD_STATUS_BELOW
    return FLOOD_STATUS_NO_CLASSIFICATION


def _parse_iso_timestamp(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class BomFloodCoordinator(DataUpdateCoordinator[dict[str, StationReading]]):

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._station_ids: list[str] = entry.data[CONF_STATIONS]
        self._station_meta: dict[str, StationMeta] = build_station_meta(entry)
        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        self._history: dict[str, list[tuple[datetime, float]]] = {}
        self._last_rates: dict[str, float] = {}
        self._rate_calculated_at: dict[str, datetime] = {}
        self._logged_missing: set[str] = set()
        self._logged_stale: set[str] = set()
        self._rejected_runs: dict[str, int] = {}
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=scan_interval),
        )

    def _rate_is_fresh(self, calculated_at: datetime) -> bool:
        return dt_util.utcnow() - calculated_at <= timedelta(hours=MAX_RATE_AGE_HOURS)

    async def async_load_history(self) -> None:
        """Restore each gauge's recent observations from disk.

        Rate of rise is measured across a window of readings. Holding them only
        in memory meant every restart discarded the baseline and left the sensor
        unknown until BOM published enough new readings — hours, on gauges that
        report infrequently. Restoring them lets the first poll after a restart
        compute a real rate instead.
        """
        stored = await self._store.async_load()
        if not stored:
            return
        for station_id, record in (stored.get("stations") or {}).items():
            observations = []
            for timestamp, level in record.get("observations") or []:
                parsed = _parse_iso_timestamp(timestamp)
                if parsed is not None and level is not None:
                    observations.append((parsed, level))
            if not observations:
                # Pre-window storage format: a single previous observation.
                timestamp = _parse_iso_timestamp(record.get("timestamp") or "")
                level = record.get("level")
                if timestamp is not None and level is not None:
                    observations = [(timestamp, level)]
            if observations:
                self._history[station_id] = sorted(observations)
            if "observations" not in record:
                # Pre-window record: its stored rate came from consecutive-sample
                # maths, which a single bad reading can inflate. Drop it and let
                # the next full window produce one.
                continue
            rate = record.get("rate")
            calculated_at = _parse_iso_timestamp(record.get("rate_calculated_at") or "")
            if rate is not None and calculated_at is not None and self._rate_is_fresh(calculated_at):
                self._last_rates[station_id] = rate
                self._rate_calculated_at[station_id] = calculated_at
        _LOGGER.debug(
            "Restored observations for %d gauge(s), %d with a usable rate",
            len(self._history),
            len(self._last_rates),
        )

    def _save_history(self) -> None:
        def snapshot() -> dict[str, Any]:
            stations: dict[str, Any] = {}
            for station_id, observations in self._history.items():
                calculated_at = self._rate_calculated_at.get(station_id)
                stations[station_id] = {
                    "observations": [[ts.isoformat(), level] for ts, level in observations],
                    "rate": self._last_rates.get(station_id),
                    "rate_calculated_at": calculated_at.isoformat() if calculated_at else None,
                }
            return {"stations": stations}

        self._store.async_delay_save(snapshot, STORAGE_SAVE_DELAY)

    def _record_observation(self, station_id: str, timestamp: datetime, level: float) -> None:
        """Append a reading and drop anything older than the history window."""
        observations = self._history.setdefault(station_id, [])
        if any(existing == timestamp for existing, _ in observations):
            return
        observations.append((timestamp, level))
        observations.sort()
        cutoff = timestamp - timedelta(hours=RATE_HISTORY_HOURS)
        self._history[station_id] = [o for o in observations if o[0] >= cutoff]

    def _vet_reading(
        self, station_id: str, timestamp: datetime, level: float
    ) -> tuple[datetime, float, float | None]:
        """Screen a reading for physical plausibility.

        Returns the (timestamp, level) to use plus the rejected value, if any.
        A reading implying more than MAX_PLAUSIBLE_RATE_M_PER_H against the last
        accepted one is not a flood — it is a broken sample — so the last good
        reading is held instead and the gauge ages towards stale, which is the
        honest signal that usable data has stopped arriving.
        """
        observations = self._history.get(station_id)
        if not observations:
            return timestamp, level, None
        last_ts, last_level = observations[-1]
        hours = (timestamp - last_ts).total_seconds() / 3600
        if hours <= 0:
            return timestamp, level, None
        if abs(level - last_level) / hours <= MAX_PLAUSIBLE_RATE_M_PER_H:
            self._rejected_runs.pop(station_id, None)
            return timestamp, level, None

        run = self._rejected_runs.get(station_id, 0) + 1
        self._rejected_runs[station_id] = run
        if run >= DATUM_SHIFT_SAMPLES:
            _LOGGER.warning(
                "%s has reported %d consecutive readings near %.2f m, far from the "
                "previous %.2f m; treating this as a datum change and accepting it",
                station_id,
                run,
                level,
                last_level,
            )
            self._history[station_id] = []
            self._rejected_runs.pop(station_id, None)
            return timestamp, level, None

        _LOGGER.warning(
            "Ignoring implausible reading for %s: %.2f m at %s implies %.0f m/h "
            "against the previous %.2f m. Holding the last good reading.",
            station_id,
            level,
            timestamp.isoformat(),
            abs(level - last_level) / hours,
            last_level,
        )
        return last_ts, last_level, level

    def _typical_cadence_hours(self, station_id: str) -> float | None:
        """Median gap between this gauge's observations, or None if too few."""
        observations = self._history.get(station_id, [])
        if len(observations) < 3:
            return None
        gaps = sorted(
            (later[0] - earlier[0]).total_seconds() / 3600
            for earlier, later in zip(observations, observations[1:])
        )
        middle = len(gaps) // 2
        if len(gaps) % 2:
            return gaps[middle]
        return (gaps[middle - 1] + gaps[middle]) / 2

    def _staleness(
        self, station_id: str, timestamp: datetime | None
    ) -> tuple[float | None, float | None, bool]:
        """(age of reading, threshold, stale) in minutes.

        Judged against the gauge's own cadence: three missed reports marks it
        stale, floored so a fast-reporting gauge is not flagged for one late
        reading, and falling back to a fixed window until enough observations
        exist to know the cadence.
        """
        if timestamp is None:
            return None, None, False
        age_hours = (dt_util.utcnow() - timestamp).total_seconds() / 3600
        cadence = self._typical_cadence_hours(station_id)
        if cadence is None:
            limit_hours = STALE_FALLBACK_HOURS
        else:
            limit_hours = max(STALE_MIN_HOURS, STALE_CADENCE_MULTIPLIER * cadence)
        return (
            round(age_hours * 60, 1),
            round(limit_hours * 60, 1),
            age_hours > limit_hours,
        )

    def _windowed_rate(
        self, station_id: str, timestamp: datetime, level: float
    ) -> float | None:
        """Rate of rise in m/hr measured against the oldest reading in the window.

        Returns None until a reading at least RATE_WINDOW_MIN_HOURS old exists,
        which is deliberate: a rate derived from two readings minutes apart is
        dominated by gauge noise, and this figure drives flood projections.
        """
        candidates = [
            (ts, lvl)
            for ts, lvl in self._history.get(station_id, [])
            if timedelta(hours=RATE_WINDOW_MIN_HOURS)
            <= timestamp - ts
            <= timedelta(hours=RATE_WINDOW_MAX_HOURS)
        ]
        if not candidates:
            return None
        baseline_ts, baseline_level = min(candidates)
        hours = (timestamp - baseline_ts).total_seconds() / 3600
        if hours <= 0:
            return None
        return round((level - baseline_level) / hours, 3)

    def _cached_rate(self, station_id: str) -> float | None:
        """Last computed rate, unless it has aged out."""
        calculated_at = self._rate_calculated_at.get(station_id)
        if calculated_at is None or not self._rate_is_fresh(calculated_at):
            self._last_rates.pop(station_id, None)
            self._rate_calculated_at.pop(station_id, None)
            return None
        return self._last_rates.get(station_id)

    async def _async_update_data(self) -> dict[str, StationReading]:
        # Fetch water level and hourly rainfall HCS files in parallel
        try:
            wl_content, rain_content = await asyncio.gather(
                self.hass.async_add_executor_job(_fetch_latest_hcs),
                self.hass.async_add_executor_job(_fetch_latest_rainfall_hcs),
                return_exceptions=True,
            )
        except Exception as exc:
            raise UpdateFailed(f"BOM FTP error: {exc}") from exc

        if isinstance(wl_content, Exception):
            raise UpdateFailed(f"Water level fetch failed: {wl_content}")

        if isinstance(rain_content, Exception):
            _LOGGER.warning("Rainfall fetch failed (non-critical): %s", rain_content)
            rain_content = ""

        try:
            wl_data = _parse_hcs(wl_content, "WL")
        except Exception as exc:
            raise UpdateFailed(f"HCS parse error: {exc}") from exc

        rain_data = {}
        if rain_content:
            try:
                rain_data = _parse_hcs(rain_content, "RN")
            except Exception:
                _LOGGER.warning("Rainfall HCS parse failed", exc_info=True)

        readings: dict[str, StationReading] = {}
        missing: list[str] = []
        for station_id in self._station_ids:
            if station_id not in wl_data:
                missing.append(station_id)
                continue

            level, timestamp, quality = wl_data[station_id]
            meta = self._station_meta.get(station_id)
            curr_ts = _parse_iso_timestamp(timestamp)

            # Screen out physically impossible readings before they reach the
            # level, the rate, the projections or the flood status.
            rejected: float | None = None
            if level is not None and curr_ts is not None:
                curr_ts, level, rejected = self._vet_reading(station_id, curr_ts, level)
                timestamp = curr_ts.isoformat().replace("+00:00", "Z")

            # Rate of rise (m/hr) measured across a window of BOM observations,
            # not between consecutive ones — see RATE_WINDOW_MIN_HOURS.
            rate_of_rise: float | None = None
            if level is not None and curr_ts is not None:
                windowed = self._windowed_rate(station_id, curr_ts, level)
                self._record_observation(station_id, curr_ts, level)
                if windowed is not None:
                    rate_of_rise = windowed
                    self._last_rates[station_id] = windowed
                    self._rate_calculated_at[station_id] = curr_ts
                else:
                    # Not enough spread yet, or BOM has not published a new
                    # reading — keep the rate the last full window produced.
                    rate_of_rise = self._cached_rate(station_id)
            elif level is not None:
                rate_of_rise = self._cached_rate(station_id)
            else:
                self._last_rates.pop(station_id, None)
                self._rate_calculated_at.pop(station_id, None)

            # Trend follows the same windowed rate, so a single bad reading
            # cannot flip a falling gauge to "rising".
            if rate_of_rise is None:
                trend = TREND_STEADY
            elif rate_of_rise > TREND_THRESHOLD_M:
                trend = TREND_RISING
            elif rate_of_rise < -TREND_THRESHOLD_M:
                trend = TREND_FALLING
            else:
                trend = TREND_STEADY

            flood_status = _derive_flood_status(level, meta)

            age_min, stale_after_min, is_stale = self._staleness(station_id, curr_ts)

            rainfall: float | None = None
            if station_id in rain_data:
                rainfall = rain_data[station_id][0]

            readings[station_id] = StationReading(
                station_id=station_id,
                level=level,
                timestamp=timestamp,
                quality=quality,
                trend=trend,
                flood_status=flood_status,
                rate_of_rise_m_per_hr=rate_of_rise,
                rainfall_mm_1h=rainfall,
                rate_calculated_at=(
                    calculated_at.isoformat()
                    if (calculated_at := self._rate_calculated_at.get(station_id))
                    else None
                ),
                reading_age_min=age_min,
                stale_after_min=stale_after_min,
                stale=is_stale,
                rejected_reading=rejected,
            )
            if is_stale and station_id not in self._logged_stale:
                _LOGGER.warning(
                    "BOM has not published a new reading for %s in %.0f minutes; "
                    "the level shown is that old reading, not a current one",
                    meta.name if meta else station_id,
                    age_min or 0,
                )
                self._logged_stale.add(station_id)
            elif not is_stale:
                self._logged_stale.discard(station_id)

        if missing and not readings:
            # Every configured gauge is absent from the file. Failing loudly beats
            # returning an empty result, which leaves every entity unavailable with
            # nothing in the log to explain why.
            raise UpdateFailed(
                f"None of the {len(missing)} configured gauge(s) were found in the "
                f"BOM water level file ({HCS_WATER_LEVEL_PREFIX}); "
                f"missing: {', '.join(missing)}"
            )
        if missing and not self._logged_missing.issuperset(missing):
            _LOGGER.warning(
                "No BOM water level data for gauge(s): %s. The remaining %d gauge(s) "
                "are updating normally.",
                ", ".join(sorted(missing)),
                len(readings),
            )
        self._logged_missing = set(missing)
        self._save_history()

        return readings
