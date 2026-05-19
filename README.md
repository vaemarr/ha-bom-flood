# BOM Flood Monitor

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that monitors river gauge levels and flood status from the [Bureau of Meteorology](https://www.bom.gov.au/) (BOM) for Australia.

## Features

- **Water level sensor** — live gauge height in metres, updated every 15 minutes
- **Flood status sensor** — enumerated state: `Below Flood Level`, `Minor`, `Moderate`, `Major`, or `Unknown`
- **Rate of rise sensor** — water level change in m/hr (positive = rising, negative = falling); retains last known value between BOM update cycles
- **Hourly rainfall sensor** — rainfall in mm for gauges that report it
- **Time-to-flood attributes** — when a gauge is actively rising, the water level sensor exposes `time_to_minor_flood_hr`, `time_to_moderate_flood_hr`, and `time_to_major_flood_hr` (hours until each threshold is reached at the current rate)
- **Per-gauge devices** — each river gauge appears as a separate device with all four sensors attached
- Coverage across **QLD, NSW, VIC, SA, and TAS**
- No API key required — uses BOM's anonymous public FTP

## How It Works

BOM publishes two data products on their anonymous FTP at `ftp.bom.gov.au/anon/gen/fwo/`:

| Product | Purpose |
|---|---|
| `IDQ65910_*.hcs` | National water level readings, updated every ~15 minutes |
| `IDQ65901_*.hcs` | National hourly rainfall readings |
| `IDQ65404.html` etc. | Regional HTML flood maps — used once at setup for station discovery |

During setup the integration fetches the HTML flood map for your selected region to discover station names, coordinates, and flood thresholds (minor/moderate/major levels in metres). This metadata is stored in the config entry — no HTML parsing happens at runtime.

Every poll cycle the coordinator fetches the latest HCS files over FTP (in parallel), parses the CSV data, and updates all sensor entities. No external dependencies — only Python stdlib (`ftplib`, `csv`, `re`).

## Supported Regions

| State | Regions |
|---|---|
| **QLD** | Southeast QLD, SE QLD Extended, Wide Bay/Burnett, North QLD, Lockyer Valley/Darling Downs, Bowen/Proserpine, Townsville/Cairns |
| **NSW** | South Coast, North West, Hunter Valley, Inland Central, Central West, Mid North Coast |
| **VIC** | Far South East/Gippsland, East Gippsland, Melbourne Metro/Western, South West/Grampians, Far West/Murray-Darling |
| **SA** | Adelaide Metro/Central, Adelaide/South |
| **TAS** | Northern/Central Tasmania, Northern/Central Tasmania Extended |

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → three-dot menu → **Custom repositories**
2. Add `https://github.com/vaemarr/ha-bom-flood` as an **Integration**
3. Search for **BOM Flood Monitor** and install
4. Restart Home Assistant

### Manual

1. Copy `custom_components/bom_flood/` into your HA config `custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **BOM Flood Monitor**
3. Select your **state**, then your **region**
4. Choose one or more **river gauges** to monitor
5. Set the **update interval** (5–60 minutes; BOM updates approximately every 15 minutes)

You can add multiple instances (one per region) if you need gauges from different regions.

## Sensors

Each gauge gets four entities:

| Entity | Unit | Notes |
|---|---|---|
| `Water Level` | m | Primary sensor; includes all attributes |
| `Flood Status` | — | Enum: Below Flood Level / Minor / Moderate / Major / Unknown |
| `Rate of Rise` | m/h | Positive = rising; retains last value when BOM hasn't updated |
| `Hourly Rainfall` | mm | Unavailable if BOM doesn't report rainfall for this gauge |

### Water Level Attributes

| Attribute | Description |
|---|---|
| `flood_status` | Current flood classification |
| `trend` | `rising`, `falling`, or `steady` |
| `last_observed` | ISO 8601 timestamp of the BOM reading |
| `quality` | BOM data quality flag (1 = good) |
| `minor_flood_level_m` | Minor flood threshold (metres) |
| `moderate_flood_level_m` | Moderate flood threshold (metres) |
| `major_flood_level_m` | Major flood threshold (metres) |
| `latitude` / `longitude` | Gauge coordinates |
| `time_to_minor_flood_hr` | Hours until minor flood at current rate (only when rising and below threshold) |
| `time_to_moderate_flood_hr` | Hours until moderate flood at current rate |
| `time_to_major_flood_hr` | Hours until major flood at current rate |

## Automation Examples

**Alert when a gauge reaches minor flood:**
```yaml
trigger:
  - platform: state
    entity_id: sensor.logan_river_at_waterford_flood_status
    to: "Minor"
action:
  - service: notify.mobile_app
    data:
      message: "Logan River at Waterford has reached minor flood level."
```

**Alert when flood status escalates (any level increase):**
```yaml
trigger:
  - platform: state
    entity_id: sensor.logan_river_at_waterford_flood_status
condition:
  - condition: template
    value_template: >
      {% set order = ['Below Flood Level', 'Minor', 'Moderate', 'Major'] %}
      {{ order.index(trigger.to_state.state) > order.index(trigger.from_state.state) }}
action:
  - service: notify.mobile_app
    data:
      message: >
        Logan River at Waterford escalated to {{ trigger.to_state.state }} flood.
```

**All clear — flood has receded:**
```yaml
trigger:
  - platform: state
    entity_id: sensor.logan_river_at_waterford_flood_status
    to: "Below Flood Level"
    from:
      - "Minor"
      - "Moderate"
      - "Major"
action:
  - service: notify.mobile_app
    data:
      message: "Logan River at Waterford is back below flood level."
```

**Alert when major flood is less than 2 hours away:**
```yaml
trigger:
  - platform: numeric_state
    entity_id: sensor.logan_river_at_waterford_water_level
    attribute: time_to_major_flood_hr
    below: 2
action:
  - service: notify.mobile_app
    data:
      message: "Major flooding at Logan River at Waterford in under 2 hours."
```

**Alert on rapid rise (rate of rise exceeds threshold):**
```yaml
trigger:
  - platform: numeric_state
    entity_id: sensor.logan_river_at_waterford_rate_of_rise
    above: 0.5
    for:
      minutes: 15
action:
  - service: notify.mobile_app
    data:
      message: >
        Logan River at Waterford rising rapidly at
        {{ states('sensor.logan_river_at_waterford_rate_of_rise') }} m/hr.
```

**Hourly summary during a flood event:**
```yaml
trigger:
  - platform: time_pattern
    hours: "/1"
condition:
  - condition: not
    conditions:
      - condition: state
        entity_id: sensor.logan_river_at_waterford_flood_status
        state: "Below Flood Level"
action:
  - service: notify.mobile_app
    data:
      message: >
        Flood update — Logan River at Waterford: {{ states('sensor.logan_river_at_waterford_water_level') }} m
        ({{ states('sensor.logan_river_at_waterford_flood_status') }},
        {{ states('sensor.logan_river_at_waterford_trend') }}).
```


**Alert if a gauge stops reporting (potential sensor outage):**
```yaml
trigger:
  - platform: state
    entity_id: sensor.logan_river_at_waterford_water_level
    to: "unavailable"
    for:
      minutes: 30
action:
  - service: notify.mobile_app
    data:
      message: "Logan River at Waterford gauge has been unavailable for 30 minutes."
```

## Options

After setup, click **Configure** on the integration to change:
- Which gauges are monitored
- The update interval

## License

MIT
