from __future__ import annotations

DOMAIN = "bom_flood"
PLATFORMS = ["binary_sensor", "sensor"]
DEFAULT_SCAN_INTERVAL = 15  # minutes

FTP_HOST = "ftp.bom.gov.au"
FTP_PATH = "/anon/gen/fwo/"
# IDZ* are the national products. The per-state equivalents (IDQ/IDN/IDV/IDS/IDT)
# only carry gauges in that state, and border region maps span two states, so the
# national files are the only ones that cover every gauge offered during setup.
HCS_WATER_LEVEL_PREFIX = "IDZ65910"
HCS_RAINFALL_HOURLY_PREFIX = "IDZ65901"
# Rain in 15-minute periods, published every 15 minutes. Each record covers the
# 15 minutes up to its timestamp (checked: four sum to the hourly total).
HCS_RAINFALL_15MIN_PREFIX = "IDZ65900"
# Every water level reading from the last ~3 hours, published hourly. Seeds the
# history after a restart and fills gaps between the 15-minute files.
HCS_WATER_LEVEL_DENSE_PREFIX = "IDZ65911"

FTP_TIMEOUT_S = 30
FETCH_ATTEMPTS = 3
FETCH_RETRY_DELAYS_S = (2, 5)
# Hard ceiling on one whole update, so a stalled transfer cannot freeze the
# displayed values while the coordinator still reports success.
UPDATE_TIMEOUT_S = 180
# After a failed fetch the last readings stay on display (ageing, and going
# stale) for this long before the entities become unavailable.
KEEP_SERVING_HOURS = 6
# How often the regional map is re-read for revised flood levels and BOM's own
# displayed status.
MAP_REFRESH_HOURS = 1

BOM_STATES: dict[str, str] = {
    "QLD": "Queensland",
    "NSW": "New South Wales",
    "VIC": "Victoria",
    "SA":  "South Australia",
    "TAS": "Tasmania",
}

# Labels describe what each map actually covers, from its gauges' rivers and
# coordinates (checked 2026-10-19). Several maps cross state borders: a gauge
# can appear on more than one map, and border rivers sit under either state.
BOM_REGIONS: dict[str, dict[str, str]] = {
    "QLD": {
        "IDQ65404": "South East Queensland: Logan, Albert, Bremer, Gold Coast",
        "IDQ65412": "South East Queensland: Brisbane, Lockyer, Mary, Logan",
        "IDQ65420": "Wide Bay and Sunshine Coast: Mary, Burnett, Maroochy",
        "IDQ65428": "Darling Downs and Granite Belt: Condamine, Macintyre, Balonne",
        "IDQ65323": "Central and Western Queensland: Burdekin, Nogoa, Warrego, Thomson",
        "IDQ65436": "North Queensland: Townsville, Mackay, Bowen",
        "IDQ65444": "Far North Queensland: Cairns, Herbert, Barron, Johnstone",
    },
    "NSW": {
        "IDN65190": "Northern Rivers and Gold Coast: Tweed, Richmond, Clarence",
        "IDN65191": "Mid North Coast and Hunter: Macleay, Manning, Hastings, Hunter",
        "IDN65193": "Sydney, Hawkesbury and Illawarra: Hawkesbury, Nepean, Georges, Shoalhaven",
        "IDN65194": "South Coast and Snowy: Shoalhaven, Moruya, Bega, Snowy",
        "IDN65195": "Northern Inland: Namoi, Gwydir, Macintyre, Barwon",
        "IDN65196": "Central West: Lachlan, Macquarie, Bogan",
        "IDN65197": "Riverina and Murray: Murrumbidgee, Murray, Lachlan",
        "IDN65198": "Far West: Darling, Warrego, Culgoa, lower Lachlan",
        "IDN65338": "Coffs Harbour: Coffs Creek",
    },
    "VIC": {
        "IDV65255": "Gippsland and North East: Snowy, Mitchell, Thomson, upper Murray",
        "IDV65263": "Melbourne East and West Gippsland: Yarra, Latrobe, Thomson, King",
        "IDV65271": "Melbourne West and Geelong: Werribee, Maribyrnong, Barwon",
        "IDV65279": "Western Victoria and Wimmera: Wimmera, Glenelg, Avoca, Loddon",
        "IDV65287": "Northern Victoria and Murray: Murray, Goulburn, Campaspe, Loddon",
    },
    "SA": {
        "IDS65054": "Adelaide Hills and Metro: Torrens, Onkaparinga, Sturt",
        "IDS65072": "Adelaide and Fleurieu: Onkaparinga, Torrens, Lake Alexandrina",
    },
    "TAS": {
        "IDT65305": "North and Central Tasmania: Mersey, Meander, South Esk",
        "IDT65315": "Midlands and South East Tasmania: South Esk, Derwent, Macquarie",
        "IDT65325": "Southern Tasmania: Derwent, Huon, South Esk",
    },
}

def warnings_url(state: str) -> str:
    """BOM's flood warnings page for a state (checked live after BOM's 2025 rebuild)."""
    return f"https://www.bom.gov.au/{state.lower()}/flood/" if state else "https://www.bom.gov.au/australia/flood/"


CONF_STATE = "state"
CONF_REGION = "region"
CONF_STATIONS = "stations"
CONF_STATION_META = "station_meta"
CONF_SCAN_INTERVAL = "scan_interval"
# User-supplied flood thresholds, keyed by station id, for gauges BOM does not
# classify: {"540790": {"minor_m": 1.5, "moderate_m": 2.0, "major_m": 2.5}}
CONF_CUSTOM_THRESHOLDS = "custom_thresholds"

THRESHOLD_KEYS = ("minor_m", "moderate_m", "major_m")
LEVEL_KEYS = ("minor", "moderate", "major")

THRESHOLDS_SOURCE_BOM = "bom"
THRESHOLDS_SOURCE_USER = "user"


# Stable state keys for the Flood Status sensor. Displayed text comes from
# translations ("Below flood level", "Minor flood", ...), so automations can match
# on these keys without depending on wording. "no_classification" is a working
# gauge BOM sets no flood levels for — distinct from the state being unknown,
# which means there is no reading to classify.
FLOOD_STATUS_BELOW = "below_flood_level"
FLOOD_STATUS_MINOR = "minor"
FLOOD_STATUS_MODERATE = "moderate"
FLOOD_STATUS_MAJOR = "major"
FLOOD_STATUS_NO_CLASSIFICATION = "no_classification"
FLOOD_STATUS_OPTIONS = [
    FLOOD_STATUS_BELOW,
    FLOOD_STATUS_MINOR,
    FLOOD_STATUS_MODERATE,
    FLOOD_STATUS_MAJOR,
    FLOOD_STATUS_NO_CLASSIFICATION,
]
# Ordinal severity, for sorting and "worst gauge" templates.
FLOOD_SEVERITY = {
    FLOOD_STATUS_NO_CLASSIFICATION: 0,
    FLOOD_STATUS_BELOW: 1,
    FLOOD_STATUS_MINOR: 2,
    FLOOD_STATUS_MODERATE: 3,
    FLOOD_STATUS_MAJOR: 4,
}

TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_STEADY = "steady"
# A tidal gauge moving within its usual tide range: the rise or fall is the tide.
TREND_TIDAL = "tidal"
TREND_OPTIONS = [TREND_RISING, TREND_STEADY, TREND_FALLING, TREND_TIDAL]
# A gauge is "rising" above TREND_THRESHOLD_M_PER_H and "falling" below
# -TREND_FALLING_THRESHOLD_M_PER_H; in between it is "steady", and no flood
# projection is made. The band is deliberately asymmetric: missing a rise is
# the dangerous error, so rises are reported sooner. Calibrated against BOM's
# own displayed trend (tools/validate.py): 95% agreement.
TREND_THRESHOLD_M_PER_H = 0.01
TREND_FALLING_THRESHOLD_M_PER_H = 0.02

STORAGE_VERSION = 1
STORAGE_SAVE_DELAY = 30

# Rate of rise is a robust (Theil-Sen) slope over the last RATE_WINDOW_HOURS of
# accepted readings, widened up to RATE_WINDOW_MAX_HOURS (or 2.5 reporting
# intervals, for slow gauges) until there are enough readings. It is not
# reported until the readings span RATE_MIN_SPAN_HOURS. BOM occasionally
# publishes a single wrong value — a gauge steady at 1.71 m reported 0.81 m once
# — and a median of pairwise slopes is not dragged around by one such sample.
RATE_WINDOW_HOURS = 1.0
RATE_WINDOW_MAX_HOURS = 2.0
RATE_MIN_SPAN_HOURS = 0.5
RATE_HISTORY_HOURS = 8.0
# Readings kept per gauge: enough for tide detection, which needs over a day.
HISTORY_HOURS = 40.0

# Tidal gauges (Logan River at Logan Village swings ~0.6 m twice a day; Oct
# 2026 it sent three false heads-ups). Detected from the readings: a 12.42 h
# (+ 6.21 h) sinusoid on top of a smooth curve explains TIDE_MIN_FIT of the
# remaining variation over the last 40 h, with a range of TIDE_MIN_RANGE_M or
# more, continuously for TIDE_CONFIRM_HOURS. Off again as soon as the fit drops
# below TIDE_OFF_FIT or the range below TIDE_OFF_RANGE_M: a flood swamps the
# tide, and the gauge is then treated like any other. One-off matches happen
# during floods (15 of 82,589 40 h windows over ten years of 23 non-tidal SEQ
# gauges), which is why one match is never enough. Live Logan Village matched
# 7 of 7 windows (0.72-0.95); the other live gauges 0 of 23.
TIDE_PERIOD_HOURS = 12.42
TIDE_MIN_SPAN_HOURS = 30.0
TIDE_MIN_FIT = 0.6
TIDE_MIN_RANGE_M = 0.15
TIDE_CONFIRM_HOURS = 12.0
TIDE_OFF_FIT = 0.4
TIDE_OFF_RANGE_M = 0.1
# While the level stays within the highs and lows of the previous day (readings
# 3 to 25 h old) plus this margin, its rise or fall is taken as tide: trend
# "tidal", and no time-to-flood or heads-up. Above that, it is the river.
TIDE_BAND_HOURS = (3.0, 25.0)
TIDE_BAND_MARGIN_M = 0.15

# Flood projections beyond this many hours are not shown.
PROJECTION_HORIZON_HOURS = 48.0

# "Earliest likely" time to a flood level: the straight-line projection scaled
# down by a factor learnt from six historical floods (2011-2022, 1,115 threshold
# crossings, tools/backtest_floods.py). The factor is the 3rd percentile of
# actual / projected time, by projection length, so the level was reached no
# earlier than the time shown in 93.6% of cases on floods held out of the
# calibration (worst single flood 90.5%). Interpolated in log-hours between the
# midpoints of the calibration bands, which keeps the result monotonic.
EARLIEST_BAND_MIDPOINTS_H = (0.5, 1.41, 2.83, 5.66, 11.3, 27.7)
EARLIEST_FACTORS = (0.262, 0.379, 0.367, 0.33, 0.252, 0.157)

# Physical plausibility of a step between two readings. Not outlier statistics:
# a flash flood genuinely looks like a sudden jump, so rises are given a generous
# bound. Falls are physically slower and are the usual shape of a sensor fault
# (dropouts, -26 m readings), so they get a tighter one. Elapsed time is floored
# so readings a minute apart are not judged as an absurd rate, and capped so a
# long gap does not make any value at all "plausible".
MAX_RISE_FIRST_HOUR_M = 10.0
MAX_RISE_M_PER_H_AFTER = 4.0
MAX_FALL_M_PER_H = 3.0
MIN_STEP_HOURS = 0.25
MAX_STEP_HOURS = 6.0
# Consecutive implausible readings — distinct BOM observations that agree with
# each other — after which the gauge is taken to have genuinely changed level
# (re-levelled, or a rise beyond the bound above) and the new level is accepted.
DATUM_SHIFT_SAMPLES = 3
# After a gauge changes datum (LGH <-> AHD), its flood status is withheld until
# BOM's map shows a level within this of ours, i.e. on the same datum.
DATUM_AGREEMENT_M = 0.5

# A gauge that goes quiet is more dangerous than a noisy one: the last reading
# keeps being displayed as though it were current. Staleness is judged against
# each gauge's own cadence, since BOM gauges report anywhere from every few
# minutes to every few hours.
#
# Cadence alone misleads: during rain a gauge may log a reading every few
# minutes while BOM still publishes once an hour, ~45 min late (Coomera at
# Canungra, Oct 2026: flagged out of date for half of every hour). So once a
# few publications have been seen, the limit is STALE_ARRIVAL_MULTIPLIER x the
# usual age of the newest reading just before the next one turns up.
STALE_CADENCE_MULTIPLIER = 3
STALE_MIN_HOURS = 1.0
STALE_FALLBACK_HOURS = 3.0
STALE_ARRIVAL_MULTIPLIER = 1.5
STALE_ARRIVAL_SAMPLES = 12  # publications remembered
STALE_ARRIVAL_MIN_SAMPLES = 3
# Cap, so a feed that has died is noticed within this long even on a gauge
# that normally reports only every few hours.
STALE_MAX_HOURS = 6.0

# A single reading that jumps more than this (per hour elapsed, at least this
# much) is held back until the next reading confirms it. BOM occasionally
# publishes one wild value (Albert River at Wolffdene 1.80 -> 2.65 -> 1.80 m;
# Cedar Creek at Plunkett Road 4.35 m against a real peak of 1.05 m, Oct 2026).
# A real rise is confirmed by the next reading, so the cost is one reading.
SPIKE_HOLD_M = 0.5

# Heads-up: a gauge may reach minor flood soon. Straight-line time to minor
# within HEADS_UP_HOURS, level already HEADS_UP_FRACTION of minor or more, on
# HEADS_UP_CONFIRM consecutive readings. Backtest, 23 SEQ gauges 2016-2026, 258
# minor floods: 94% warned, median 2.2 h ahead, ~1.5 false alerts per
# gauge-year (the earlier "earliest likely <= 3 h" rule: ~5.9). The fraction
# also stops tides at Logan Village and small rises far below minor.
HEADS_UP_HOURS = 4.0
HEADS_UP_FRACTION = 0.5
HEADS_UP_CONFIRM = 2

# An "hourly rainfall" value older than this is not shown as current.
RAINFALL_MAX_AGE_HOURS = 2.0
