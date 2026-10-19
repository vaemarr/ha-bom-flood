"""The shipped flood alert blueprint runs and fires on the right changes."""
from __future__ import annotations

import os
import shutil

from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "blueprints", "automation", "bom_flood", "flood_alert.yaml")

STATUS = "sensor.logan_flood_status"
NEXT = "sensor.logan_time_to_next_flood_level"
STALE = "binary_sensor.logan_reading_out_of_date"


async def _setup(hass):
    dest = hass.config.path("blueprints", "automation", "bom_flood")
    os.makedirs(dest, exist_ok=True)
    shutil.copy(SRC, dest)
    calls = async_mock_service(hass, "test", "notify")
    hass.states.async_set(STATUS, "below_flood_level", {"severity": 1, "friendly_name": "Logan Flood status"})
    hass.states.async_set(NEXT, "unknown")
    hass.states.async_set(STALE, "off")
    assert await async_setup_component(hass, "automation", {
        "automation": {
            "use_blueprint": {
                "path": "bom_flood/flood_alert.yaml",
                "input": {
                    "status_entity": STATUS,
                    "next_entity": NEXT,
                    "stale_entity": STALE,
                    "hours_ahead": 3,
                    "notify": [{"action": "test.notify", "data": {"title": "{{ title }}", "message": "{{ message }}"}}],
                },
            }
        }
    })
    await hass.async_block_till_done()
    return calls


async def test_escalation_and_all_clear(hass):
    calls = await _setup(hass)
    hass.states.async_set(STATUS, "minor", {"severity": 2, "friendly_name": "Logan Flood status"})
    await hass.async_block_till_done()
    assert len(calls) == 1 and calls[0].data["title"] == "Flood: Logan"

    hass.states.async_set(STATUS, "moderate", {"severity": 3, "friendly_name": "Logan Flood status"})
    await hass.async_block_till_done()
    assert len(calls) == 2

    hass.states.async_set(STATUS, "minor", {"severity": 2, "friendly_name": "Logan Flood status"})
    await hass.async_block_till_done()
    assert len(calls) == 2  # de-escalation is not an alert

    hass.states.async_set(STATUS, "below_flood_level", {"severity": 1, "friendly_name": "Logan Flood status"})
    await hass.async_block_till_done()
    assert len(calls) == 3 and calls[2].data["title"] == "All clear: Logan"


async def test_next_level_soon_fires_once_on_crossing(hass):
    calls = await _setup(hass)
    hass.states.async_set(NEXT, "5.0", {"next_level": "minor"})
    await hass.async_block_till_done()
    assert not calls
    hass.states.async_set(NEXT, "2.5", {"next_level": "minor"})
    await hass.async_block_till_done()
    assert len(calls) == 1 and "minor" in calls[0].data["message"]
    hass.states.async_set(NEXT, "2.0", {"next_level": "minor"})
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_stale_only_alerts_while_flooding(hass):
    calls = await _setup(hass)
    hass.states.async_set(STALE, "on")
    await hass.async_block_till_done()
    assert not calls
    hass.states.async_set(STALE, "off")
    hass.states.async_set(STATUS, "minor", {"severity": 2, "friendly_name": "Logan Flood status"})
    await hass.async_block_till_done()
    hass.states.async_set(STALE, "on")
    await hass.async_block_till_done()
    assert calls[-1].data["title"] == "Gauge silent: Logan"
