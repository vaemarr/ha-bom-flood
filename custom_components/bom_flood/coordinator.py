from __future__ import annotations

import asyncio
import csv
import ftplib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_SCAN_INTERVAL,
    CONF_STATION_META,
    CONF_STATIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FTP_HOST,
    FTP_PATH,
    FLOOD_STATUS_BELOW,
    FLOOD_STATUS_MAJOR,
    FLOOD_STATUS_MINOR,
    FLOOD_STATUS_MODERATE,
    FLOOD_STATUS_UNKNOWN,
    HCS_RAINFALL_HOURLY_PREFIX,
    HCS_WATER_LEVEL_PREFIX,
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


@dataclass
class StationReading:
    station_id: str
    level: float | None
    timestamp: str
    quality: int
    trend: str
    flood_status: str
    rate_of_rise_m_per_hr: float | None
    rainfall_mm_1h: float | None


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


def _derive_flood_status(level: float | None, meta: StationMeta) -> str:
    if level is None:
        return FLOOD_STATUS_UNKNOWN
    if meta.major_m is not None and level >= meta.major_m:
        return FLOOD_STATUS_MAJOR
    if meta.moderate_m is not None and level >= meta.moderate_m:
        return FLOOD_STATUS_MODERATE
    if meta.minor_m is not None and level >= meta.minor_m:
        return FLOOD_STATUS_MINOR
    if meta.minor_m is not None:
        return FLOOD_STATUS_BELOW
    return FLOOD_STATUS_UNKNOWN


def _parse_iso_timestamp(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class BomFloodCoordinator(DataUpdateCoordinator[dict[str, StationReading]]):

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._station_ids: list[str] = entry.data[CONF_STATIONS]
        self._station_meta: dict[str, StationMeta] = {
            sid: StationMeta.from_dict(d)
            for sid, d in entry.data.get(CONF_STATION_META, {}).items()
        }
        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        self._prev_levels: dict[str, float] = {}
        self._prev_timestamps: dict[str, datetime] = {}
        self._last_rates: dict[str, float] = {}
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=scan_interval),
        )

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
        for station_id in self._station_ids:
            if station_id not in wl_data:
                _LOGGER.debug("Station %s not found in HCS data", station_id)
                continue

            level, timestamp, quality = wl_data[station_id]
            meta = self._station_meta.get(station_id)
            curr_ts = _parse_iso_timestamp(timestamp)

            # Trend from level comparison
            prev_level = self._prev_levels.get(station_id)
            if level is None or prev_level is None:
                trend = TREND_STEADY
            elif level - prev_level > TREND_THRESHOLD_M:
                trend = TREND_RISING
            elif prev_level - level > TREND_THRESHOLD_M:
                trend = TREND_FALLING
            else:
                trend = TREND_STEADY

            # Rate of rise (m/hr) using HCS timestamps for accuracy
            rate_of_rise: float | None = None
            prev_ts = self._prev_timestamps.get(station_id)
            if (
                level is not None
                and prev_level is not None
                and curr_ts is not None
                and prev_ts is not None
            ):
                dt_hours = (curr_ts - prev_ts).total_seconds() / 3600
                if dt_hours > 0:
                    rate_of_rise = round((level - prev_level) / dt_hours, 3)
                    self._last_rates[station_id] = rate_of_rise
                else:
                    rate_of_rise = self._last_rates.get(station_id)

            if level is None:
                self._last_rates.pop(station_id, None)
            else:
                self._prev_levels[station_id] = level
            if curr_ts is not None:
                self._prev_timestamps[station_id] = curr_ts

            flood_status = _derive_flood_status(level, meta) if meta else FLOOD_STATUS_UNKNOWN

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
            )

        return readings
