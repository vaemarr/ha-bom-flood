# BOM Flood Monitor

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant integration for river levels and flood status from the Australian
[Bureau of Meteorology](https://www.bom.gov.au/) (BOM), covering Queensland, New South
Wales, Victoria, South Australia and Tasmania.

> **This is not a flood warning service.** It shows BOM's published gauge data and some
> simple calculations from it. Always follow official [BOM flood warnings](https://www.bom.gov.au/australia/flood/)
> and emergency services advice.

## What you get

Each gauge you pick becomes a device with these entities:

| Entity | Example | What it tells you |
|---|---|---|
| **Flood status** | `Minor flood` | BOM's classification for the current level: Below flood level, Minor, Moderate, Major, or *No flood levels set* |
| **Water level** | `2.41 m` | The latest reading BOM published |
| **Trend** | `Rising` | Rising, Steady or Falling, from the last hour of readings |
| **Rate of rise** | `+0.35 m/h` | How fast the level is changing |
| **Time to next flood level** | `1.1 h` | The **earliest likely** time the next flood level is reached, while the river is rising (see [accuracy](#how-accurate-is-it)) |
| **BOM flood warning** | `On` | A current official BOM flood warning names this gauge; attributes carry the title, BOM's forecast level and timing, and a link |
| **Last reading** | `12 minutes ago` | When BOM observed the level shown |
| **Reading out of date** | `On` | The gauge has stopped reporting. Don't trust the level as current |
| **Flooding** | `On` | The gauge is at Minor flood or above |
| **Rainfall last hour** | `4.5 mm` | For gauges that report rain (off by default for gauges that don't) |

Flood status, trend and both binary sensors use stable state values (`below_flood_level`,
`minor`, `moderate`, `major`, `no_classification`; `rising`, `steady`, `falling`) with
translated labels, so automations don't depend on wording.

## How accurate is it?

Accuracy is measured, not assumed. Two tools in [`tools/`](tools/) run the integration's
own code against BOM data:

- **`validate.py`** compares what the integration would show with what BOM itself
  publishes, using live files mirrored from BOM's FTP server and BOM's regional maps.
- **`backtest_floods.py`** replays real floods (January 2011, January 2013, March 2017,
  March 2021, February 2022, July 2022) from BOM's Water Data Online archive through the
  integration on a 15-minute polling clock, then compares each moment with what the
  river did next.

Results (October 2026):

| Check | Result | Measured on |
|---|---|---|
| Level and time shown equal BOM's published file | **100%** | 44,792 readings |
| Level shown equals BOM's own flood map | **100%** | 13,950 readings |
| Flood status equals BOM's own flood map | **100%** | 13,950 readings |
| Flood status correct throughout real floods | **99.9%** | 1.03 million replayed polls |
| Trend equals BOM's displayed trend | **95%** | 13,200 readings |
| Flood levels reached with a warning at least 1 h ahead | **93%** (typically 5.4 h ahead) | 1,115 crossings, 127 gauges, 6 floods |
| Time to next flood level (earliest likely) was not later than the real time | **93.6%** (every flood ≥ 90.5%) | projections made 0.5–6 h before a crossing, tested on floods held out of the calibration |
| A time to next flood level was showing 0.5–6 h before a crossing | 79% | the rest of the time the river was not yet rising |
| Straight-line time (`at_current_rate_h` attribute) within ±30% of the real time | 38% | same moments |

**Why "earliest likely" rather than a single time.** A straight line from the current
rate is right to within 30% only 38% of the time: the river speeds up after the
projection (31% of cases), isn't rising yet (17%), or slows down (10%). Fifteen
approaches were tested on floods held out of training: other windows, curve fitting,
a decaying rate, median calibration, a learned model, similar past moments, gradient
boosting, upstream gauges, and BOM rainfall. The best (adding rainfall) reached 39.5%;
none came close to 90%, because the missing information (rain still to fall, the
catchment's response) isn't in the gauge data. So the entity
shows a time calibrated on past floods to be early rather than late: the river reached
the level no earlier than the time shown in 93.6% of cases. It is deliberately
cautious, typically about 40% of the real time, so "1.1 h" means "possibly in about an
hour; act as if it could be". The straight-line time stays in the
`at_current_rate_h` attribute.

**BOM's official warnings come first.** When BOM issues a flood warning naming a gauge,
its forecast (level, class and timing, written by BOM's forecasters using rainfall and
catchment models) appears on the *BOM flood warning* entity. Past BOM warnings are not
archived publicly, so their accuracy could not be scored here.

**What this means in practice**

- The **level, status and trend** shown are reliable. They match BOM.
- **A warning normally comes well ahead of a flood level being reached.** In past floods
  the time-to-next-level appeared at least an hour before the level was reached in most
  cases, typically about five hours ahead.
- **The time to next flood level errs early on purpose.** Treat it as "could be this
  soon". The level is often reached later, and sometimes not at all.
- **Use BOM's official forecast when there is one**, shown on the *BOM flood warning* entity.

## How it works

BOM publishes these on its anonymous FTP server (`ftp.bom.gov.au/anon/gen/fwo/`):

| Product | Used for |
|---|---|
| `IDZ65910` | Latest water level for every gauge, every ~15 minutes |
| `IDZ65911` | Every reading from the last ~3 hours, hourly. Fills gaps and restores history after a restart |
| `IDZ65901` | Hourly rainfall |
| Flood warnings (`IDQ2xxxx`, `IDN3xxxx`, ...) | Official BOM flood warnings and watches, checked every poll |
| Regional maps (`IDQ65404.html` etc.) | Gauge names, flood levels, and BOM's own displayed status. Read at setup and again every hour |

Every poll:

1. **Download with retries.** Truncated files (shorter than their own header says) are
   rejected. If BOM can't be reached, the last readings stay on display, ageing visibly,
   for up to 6 hours before the entities become unavailable.
2. **Screen each reading.** A reading that no river could physically produce (for
   example -26 m on a gauge sitting at 0.9 m) is held back and the last good reading
   kept. Rises get generous limits (10 m in the first hour), because flash floods are
   real. If three separate readings agree on a new level, it is accepted as genuine.
3. **Measure the trend** with a robust slope over the last hour of readings (widened for
   gauges that report every few hours), so one bad sample can't fake a rise.
4. **Classify** the level against BOM's flood levels, the same way BOM's maps do.
5. **Check freshness.** Each gauge's normal reporting interval is learnt; three missed
   reports (minimum one hour) marks it out of date. Rate, trend and projections are then
   withheld, because a rate from hours ago says nothing about the river now.

### When things go wrong

| Situation | What you see |
|---|---|
| BOM briefly unreachable | Last readings stay, *Reading out of date* turns on as they age |
| BOM unreachable for over 6 hours | Entities unavailable |
| Gauge stops reporting | Level stays (it's still the last thing BOM published); *Reading out of date* on; rate, trend and time to next level go unknown |
| BOM publishes an impossible value | Ignored; `rejected_reading` attribute on Water level shows it |
| Gauge changes datum (gauge height ↔ AHD) | History restarts; flood status is withheld until BOM's map shows the gauge on the new datum |
| BOM revises a flood level | Picked up within an hour; logged |

## Installation

### HACS

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/vaemarr/ha-bom-flood` as an Integration
2. Install **BOM Flood Monitor**, then restart Home Assistant

### Manual

Copy `custom_components/bom_flood/` into your `config/custom_components/` folder and restart.

## Setup

**Settings → Devices & services → Add integration → BOM Flood Monitor**, then choose:

1. **State**
2. **Region**: one of BOM's regional flood maps. Gauges near a border can appear under either state.
3. **Gauges**: shown with BOM's station number, because some different gauges share a name.
4. **Update interval**: 15 minutes suits BOM's publishing rate.

Add the integration again for gauges in another region. **Configure** changes gauges,
interval, and flood levels for gauges BOM doesn't classify.

### Regions

| State | Maps |
|---|---|
| QLD | South East (Logan/Albert/Gold Coast; Brisbane/Lockyer/Mary), Wide Bay & Sunshine Coast, Darling Downs, Central & Western, North (Townsville/Mackay), Far North |
| NSW | Northern Rivers, Mid North Coast & Hunter, Sydney/Hawkesbury/Illawarra, South Coast & Snowy, Northern Inland, Central West, Riverina & Murray, Far West, Coffs Harbour |
| VIC | Gippsland & North East, Melbourne East, Melbourne West & Geelong, Western & Wimmera, Northern & Murray |
| SA | Adelaide Hills & Metro, Adelaide & Fleurieu (BOM's SA maps cover the Adelaide area only) |
| TAS | North & Central, Midlands & South East, Southern (incl. Huon) |

### Gauges without BOM flood levels

Many gauges, often council-run creek gauges, report a level but BOM sets no flood
levels for them. They show **No flood levels set**. If you know the levels that matter
locally, **Configure** lets you enter your own minor, moderate and major heights. The
Flood status `threshold_source` attribute then reads `user`.

> Your own levels are not official BOM classifications.

## Automations

**Flood level reached or escalating:**
```yaml
triggers:
  - trigger: state
    entity_id: sensor.logan_river_at_waterford_flood_status
    to: [minor, moderate, major]
conditions:
  - condition: template
    value_template: >
      {{ state_attr(trigger.entity_id, 'severity') | int(0) >
         (trigger.from_state.attributes.severity | int(0) if trigger.from_state else 0) }}
actions:
  - action: notify.mobile_app_phone
    data:
      title: Flood alert
      message: "Logan River at Waterford: {{ state_translated(trigger.entity_id) }}"
```

**Next flood level possibly within 2 hours:**
```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.logan_river_at_waterford_time_to_next_flood_level
    below: 2
actions:
  - action: notify.mobile_app_phone
    data:
      message: >
        Logan River at Waterford could reach
        {{ state_attr('sensor.logan_river_at_waterford_time_to_next_flood_level', 'next_level') }}
        flood level in as little as
        {{ states('sensor.logan_river_at_waterford_time_to_next_flood_level') }} h.
```

**Gauge stopped reporting during a flood:**
```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.logan_river_at_waterford_reading_out_of_date
    to: "on"
conditions:
  - condition: state
    entity_id: binary_sensor.logan_river_at_waterford_flooding
    state: "on"
actions:
  - action: notify.mobile_app_phone
    data:
      message: "Logan River at Waterford gauge has stopped reporting. Check BOM directly."
```

**All clear:**
```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.logan_river_at_waterford_flooding
    from: "on"
    to: "off"
    for: { minutes: 30 }
actions:
  - action: notify.mobile_app_phone
    data:
      message: "Logan River at Waterford is back below flood level."
```

## Dashboard

A compact card per gauge using [Mushroom](https://github.com/piitaya/lovelace-mushroom)
and [ApexCharts](https://github.com/RomRider/apexcharts-card):

```yaml
type: vertical-stack
cards:
  - type: custom:mushroom-template-card
    entity: sensor.logan_river_at_waterford_flood_status
    primary: Logan River at Waterford
    secondary: >
      {{ state_translated(entity) }} · {{ states('sensor.logan_river_at_waterford_water_level') }} m ·
      {{ state_translated('sensor.logan_river_at_waterford_trend') }}
    icon: mdi:waves
    icon_color: >
      {% if is_state('binary_sensor.logan_river_at_waterford_reading_out_of_date', 'on') %}grey
      {% else %}{{ {1: 'green', 2: 'amber', 3: 'orange', 4: 'red'}.get(state_attr(entity, 'severity'), 'grey') }}{% endif %}
  - type: custom:apexcharts-card
    graph_span: 24h
    series:
      - entity: sensor.logan_river_at_waterford_water_level
        name: Level
      - entity: sensor.logan_river_at_waterford_water_level
        attribute: minor_flood_level_m
        name: Minor
        curve: stepline
      - entity: sensor.logan_river_at_waterford_water_level
        attribute: moderate_flood_level_m
        name: Moderate
        curve: stepline
```

## Upgrading from 0.x

Version 2.0 changes some entity states and attributes:

- **Flood status values** are now keys: `Below Flood Level` → `below_flood_level`,
  `Minor` → `minor`, `No Classification` → `no_classification`, and so on. The UI still
  shows readable labels. Update any automation that compares the state to text.
- **Trend and time-to-flood** moved from Water level attributes to their own entities
  (`sensor.*_trend`, `sensor.*_time_to_next_flood_level`). The old `time_to_*_flood_hr`
  attributes are gone.
- **`stale` and `reading_age_min`** attributes are replaced by the
  `binary_sensor.*_reading_out_of_date` and `sensor.*_last_reading` entities.
- Entity IDs of existing entities don't change, and their history is kept.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component pytest-cov
.venv/bin/python -m pytest --cov
```

## License

MIT
