# BOM Flood Monitor

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that monitors river gauge levels and flood status from the [Bureau of Meteorology](https://www.bom.gov.au/) (BOM) for Australia.

## Features

- **Water level sensor** — live gauge height in metres, updated every 15 minutes
- **Flood status sensor** — enumerated state using BOM's own classifications: `Below Flood Level`, `Minor`, `Moderate`, `Major`, or `No Classification` (a working gauge BOM publishes no flood thresholds for). The state is `unknown` only when there is no reading to classify.
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
| `IDZ65910_*.hcs` | National water level readings (~2,970 gauges), updated every ~15 minutes |
| `IDZ65901_*.hcs` | National hourly rainfall readings (~3,780 stations) |
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
| `Flood Status` | — | Enum: Below Flood Level / Minor / Moderate / Major / No Classification |
| `Rate of Rise` | m/h | Positive = rising; survives restarts, expires after 6 h (see below) |
| `Hourly Rainfall` | mm | `unknown` if BOM doesn't report rainfall for this gauge (about 57% of gauges) |

### How rate of rise is measured

The rate is measured against the oldest reading in a **30 minute to 2 hour window**, not
against the previous reading. BOM occasionally publishes a single wrong value — a gauge
steady at 1.71 m reported 0.81 m once, then 1.61 m — and consecutive-sample maths turns the
rebound into a 3.2 m/h rise, which projects a flood roughly 1.4 hours away that is not
coming. Measured across a window, the same series yields 0.08 m/h and no alarm, while a
genuine sustained rise still produces a rate within about 30 minutes.

The sensor reports `unknown` until a reading at least 30 minutes old exists. That is
deliberate: a rate derived from two readings minutes apart is dominated by gauge noise, and
this figure drives the flood projections.

### Rate of rise across restarts

Rate of rise needs two gauge readings at different BOM timestamps. Those observations are
persisted to `.storage`, so a restart no longer discards the baseline: the sensor reports
its last rate immediately on startup, and the first new BOM reading produces a real
calculation rather than waiting for a second one. On gauges that report every few hours,
that is the difference between a value straight away and a blank sensor half the day.

A carried-over rate expires after **6 hours**, at which point the sensor returns to
`unknown`. A rate from six hours ago is not evidence of what the water is doing now, and on
a flood sensor a confidently wrong number is worse than no number. The `rate_calculated_at`
attribute gives the BOM observation time the current rate came from, so you can always tell
a fresh calculation from a carried-over one.

### Missing values

Entities go **`unavailable`** only when BOM stops reporting the gauge altogether. When the
gauge is in the feed but a particular value is absent, that entity reports **`unknown`**
and the others keep working.

No missing reading is ever reported as `0`. Zero is a real measurement — 89 gauges were
reading exactly 0.0 m and 241 were reading below zero in the feed at the time of writing —
so treating it as "no data" would hide a genuine level, and treating "no data" as zero
would invent one.

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
- Flood levels for gauges BOM does not classify (see below)

### Unclassified gauges

BOM publishes minor/moderate/major flood levels for most gauges, but not all. ALERT-type
stations — often council-operated creek gauges — report water level normally while BOM
sets no thresholds for them, so no flood status can be derived. BOM's own maps label
these **No Classification**, and so does this integration.

If you know the levels that matter at such a gauge, the **Configure** dialog offers a
final step listing every selected gauge BOM leaves unclassified, where you can enter your
own minor, moderate and major heights in metres. The values must increase from minor to
major; leaving them blank keeps the gauge unclassified. The Flood Status sensor exposes a
`threshold_source` attribute of `bom` or `user` so you can tell the two apart.

> These are your own figures, not official BOM classifications. A status derived from them
> is only as reliable as the levels you enter. Always follow official BOM warnings and
> emergency services advice.

## License

MIT
