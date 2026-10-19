"""A stand-in for BOM's FTP server, for tests."""
from __future__ import annotations

import ftplib
from datetime import datetime
from unittest.mock import patch

from custom_components.bom_flood import analysis as A

HEADER = (
    "# HEADER: Agency Id: BoM\n"
    "# HEADER: File Generation Date: {generated}\n"
    "# HEADER: Number of Records: {count}\n"
    "# HEADER: Data Fields: IndexNo, SensorType, SensorDataType, SiteIdType, SiteId, "
    "ObservationTimestamp, RealValue, Unit, SensorParam1, SensorParam2, Quality, Comment\n"
)


def hcs(rows: list[tuple[str, str, datetime, float | None]], datum: str = "LGH") -> str:
    """rows: (sensor type, station id, observed at, value)."""
    lines = []
    for i, (kind, sid, when, value) in enumerate(rows, start=1):
        unit, param1, param2, quality = (
            ("metres", datum, "", 1) if kind == "WL" else ("mm", "", "3600", 3)
        )
        val = "" if value is None else repr(value)
        lines.append(
            f'{i},"{kind}",1,"SR","{sid}","{when.strftime("%Y-%m-%dT%H:%M:%SZ")}",{val},'
            f'"{unit}","{param1}",{param2},{quality},""'
        )
    return HEADER.format(generated="2026-10-19T00:00:00Z", count=len(lines)) + "\n".join(lines) + "\n"


def popup(sid: str, name: str, level: float, status: str, thresholds=("2.00", "3.00", "4.00"), trend="steady") -> str:
    minor, moderate, major = thresholds
    return (
        f"PopupRiver(&quot;{name}&quot;,'{sid}','-27.9','153.1','{level:.2f}','{status}','{trend}',"
        f"'19-10-2026 10:00:00','{minor}','{moderate}','{major}','Click')"
    )


class FakeBom:
    """Holds the 'latest' file per product; tests mutate it between polls."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}  # prefix -> content
        self.names: dict[str, str] = {}  # prefix -> file name
        self.maps: dict[str, str] = {}
        self.fail_prefixes: set[str] = set()
        self.calls: list[str] = []
        self.warnings: dict[str, str] = {}  # product id -> text
        self._serial = 0

    def publish(self, prefix: str, content: str) -> None:
        self._serial += 1
        self.files[prefix] = content
        self.names[prefix] = f"{prefix}_{self._serial:014d}.hcs"

    def latest_name(self, prefix: str) -> str:
        self.calls.append(prefix)
        if prefix in self.fail_prefixes or prefix not in self.names:
            raise ftplib.error_temp("421 BOM unavailable")
        return self.names[prefix]

    def fetch_named(self, name: str, sensor_type: str) -> A.HcsFile:
        prefix = name.split("_")[0]
        if prefix in self.fail_prefixes:
            raise ftplib.error_temp("421 BOM unavailable")
        return A.parse_hcs(self.files[prefix], sensor_type)

    def fetch_map(self, product: str) -> str:
        if "map" in self.fail_prefixes or product not in self.maps:
            raise ftplib.error_temp("421 BOM unavailable")
        return self.maps[product]

    def fetch_warnings(self) -> list[A.FloodWarning]:
        if "warnings" in self.fail_prefixes:
            raise ftplib.error_temp("421 BOM unavailable")
        out = []
        for pid, text in sorted(self.warnings.items()):
            w = A.parse_flood_warning(pid, text)
            if w:
                out.append(w)
        return out

    def patch(self):
        base = "custom_components.bom_flood.coordinator"
        return _Patches(
            patch(f"{base}._latest_name", side_effect=self.latest_name),
            patch(f"{base}._fetch_named", side_effect=self.fetch_named),
            patch(f"{base}._fetch_html_map", side_effect=self.fetch_map),
            patch(f"{base}._fetch_flood_warnings", side_effect=self.fetch_warnings),
            patch(f"{base}.time.sleep"),
        )


class _Patches:
    def __init__(self, *patches) -> None:
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for p in reversed(self._patches):
            p.stop()
