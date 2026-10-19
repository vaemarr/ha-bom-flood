"""The FTP layer: listing, download, retries, cleanup."""
from __future__ import annotations

import ftplib
from unittest.mock import patch

import pytest

from custom_components.bom_flood import coordinator as C

from .fake_bom import hcs
from .test_integration import NOW


class FakeFTP:
    files: dict[str, bytes] = {}
    fail_times = 0
    instances: list[FakeFTP] = []

    def __init__(self, host, timeout):
        assert host == "ftp.bom.gov.au" and timeout == 30
        if FakeFTP.fail_times:
            FakeFTP.fail_times -= 1
            raise OSError("connection refused")
        self.closed = False
        FakeFTP.instances.append(self)

    def login(self):
        pass

    def cwd(self, path):
        assert path == "/anon/gen/fwo/"

    def nlst(self, pattern):
        prefix = pattern.split("*")[0]
        return [n for n in self.files if n.startswith(prefix)]

    def retrbinary(self, cmd, callback):
        name = cmd.split(" ", 1)[1]
        if name not in self.files:
            raise ftplib.error_perm("550 not found")
        callback(self.files[name])

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_ftp():
    FakeFTP.files = {
        "IDZ65910_20261019093800.hcs": hcs([("WL", "1", NOW, 1.0)]).encode(),
        "IDZ65910_20261019095300.hcs": hcs([("WL", "1", NOW, 1.5)]).encode(),
        "IDQ65404.html": b"PopupRiver(&quot;A River at B&quot;,'1','-27','153','1.0','Below Flood Level','steady','x','2','3','4','c')",
    }
    FakeFTP.fail_times = 0
    FakeFTP.instances = []
    with patch.object(C.ftplib, "FTP", FakeFTP), patch.object(C.time, "sleep"):
        yield


def test_latest_name_and_fetch():
    name = C._latest_name("IDZ65910")
    assert name == "IDZ65910_20261019095300.hcs"
    assert C._fetch_named(name, "WL").latest("1").value == 1.5
    assert all(f.closed for f in FakeFTP.instances)


def test_no_files_is_an_error():
    with pytest.raises(ftplib.error_perm):
        C._latest_name("IDZ65999")


def test_retries_transient_failures():
    FakeFTP.fail_times = 2
    assert C._with_retries(C._latest_name, "IDZ65910").startswith("IDZ65910")


def test_gives_up_after_attempts():
    FakeFTP.fail_times = 5
    with pytest.raises(OSError):
        C._with_retries(C._latest_name, "IDZ65910")


def test_region_map():
    stations = C.fetch_region_map("IDQ65404")
    assert stations[0].name == "A River at B" and stations[0].minor_m == 2.0


def test_connection_closed_even_when_download_fails():
    with pytest.raises(ftplib.error_perm):
        C._fetch_named("missing.hcs", "WL")
    assert FakeFTP.instances[-1].closed


def test_optional_feed_returns_none_on_failure_or_unchanged():
    assert C.BomFloodCoordinator._fetch_optional("IDZ65999", "WL", None) is None
    name = "IDZ65910_20261019095300.hcs"
    assert C.BomFloodCoordinator._fetch_optional("IDZ65910", "WL", name) is None
    got = C.BomFloodCoordinator._fetch_optional("IDZ65910", "WL", "older")
    assert got[0] == name
