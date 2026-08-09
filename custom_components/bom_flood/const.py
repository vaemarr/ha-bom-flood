from __future__ import annotations

DOMAIN = "bom_flood"
PLATFORMS = ["sensor"]
DEFAULT_SCAN_INTERVAL = 15  # minutes

FTP_HOST = "ftp.bom.gov.au"
FTP_PATH = "/anon/gen/fwo/"
# IDZ* are the national products. The per-state equivalents (IDQ/IDN/IDV/IDS/IDT)
# only carry gauges in that state, and border region maps span two states, so the
# national files are the only ones that cover every gauge offered during setup.
HCS_WATER_LEVEL_PREFIX = "IDZ65910"
HCS_RAINFALL_HOURLY_PREFIX = "IDZ65901"

BOM_STATES: dict[str, str] = {
    "QLD": "Queensland",
    "NSW": "New South Wales",
    "VIC": "Victoria",
    "SA":  "South Australia",
    "TAS": "Tasmania",
}

BOM_REGIONS: dict[str, dict[str, str]] = {
    "QLD": {
        "IDQ65404": "Southeast Queensland (Brisbane, Logan, Gold Coast)",
        "IDQ65412": "Southeast Queensland Extended (Sunshine Coast to Gold Coast)",
        "IDQ65420": "Wide Bay / Burnett (Bundaberg, Maryborough)",
        "IDQ65323": "North Queensland (Mackay, Far North)",
        "IDQ65428": "Lockyer Valley / Darling Downs (Ipswich, Toowoomba)",
        "IDQ65436": "North Queensland (Bowen, Proserpine)",
        "IDQ65444": "Far North Queensland (Townsville, Cairns)",
    },
    "NSW": {
        "IDN65190": "South Coast / Far South Coast",
        "IDN65191": "North West (Namoi, Narrabri)",
        "IDN65193": "Hunter Valley (Newcastle, Maitland)",
        "IDN65195": "Inland Central (Condamine, Macintyre)",
        "IDN65197": "Central West (Lachlan, Macquarie)",
        "IDN65338": "Mid North Coast (Coffs Harbour)",
    },
    "VIC": {
        "IDV65255": "Far South East / Gippsland",
        "IDV65263": "East Gippsland / Inland",
        "IDV65271": "Melbourne Metro / Western Victoria",
        "IDV65279": "South West / Grampians",
        "IDV65287": "Far West / Murray-Darling",
    },
    "SA": {
        "IDS65054": "Adelaide Metro / Central",
        "IDS65072": "Adelaide / South",
    },
    "TAS": {
        "IDT65305": "Northern / Central Tasmania",
        "IDT65315": "Northern / Central Tasmania (Extended)",
    },
}

CONF_STATE = "state"
CONF_REGION = "region"
CONF_STATIONS = "stations"
CONF_STATION_META = "station_meta"
CONF_SCAN_INTERVAL = "scan_interval"
# User-supplied flood thresholds, keyed by station id, for gauges BOM does not
# classify: {"540790": {"minor_m": 1.5, "moderate_m": 2.0, "major_m": 2.5}}
CONF_CUSTOM_THRESHOLDS = "custom_thresholds"

THRESHOLD_KEYS = ("minor_m", "moderate_m", "major_m")

THRESHOLDS_SOURCE_BOM = "bom"
THRESHOLDS_SOURCE_USER = "user"

ATTR_FLOOD_STATUS = "flood_status"
ATTR_TREND = "trend"
ATTR_LAST_OBSERVED = "last_observed"
ATTR_MINOR_LEVEL = "minor_flood_level_m"
ATTR_MODERATE_LEVEL = "moderate_flood_level_m"
ATTR_MAJOR_LEVEL = "major_flood_level_m"
ATTR_STATION_ID = "station_id"
ATTR_LATITUDE = "latitude"
ATTR_LONGITUDE = "longitude"
ATTR_QUALITY = "quality"
ATTR_RATE_OF_RISE = "rate_of_rise_m_per_hr"
ATTR_RAINFALL_1H = "rainfall_mm_1h"
ATTR_RATE_CALCULATED_AT = "rate_calculated_at"
ATTR_THRESHOLD_SOURCE = "threshold_source"
ATTR_TIME_TO_MINOR = "time_to_minor_flood_hr"
ATTR_TIME_TO_MODERATE = "time_to_moderate_flood_hr"
ATTR_TIME_TO_MAJOR = "time_to_major_flood_hr"

# Matches the vocabulary BOM uses on its own flood maps. "No Classification" is
# what BOM publishes for a working gauge it has not set flood thresholds for —
# distinct from the state being unknown, which means we have no reading at all.
FLOOD_STATUS_BELOW = "Below Flood Level"
FLOOD_STATUS_MINOR = "Minor"
FLOOD_STATUS_MODERATE = "Moderate"
FLOOD_STATUS_MAJOR = "Major"
FLOOD_STATUS_NO_CLASSIFICATION = "No Classification"

# Previous observations are persisted so a restart does not throw away the
# baseline the rate of rise is measured from. Beyond MAX_RATE_AGE_HOURS a cached
# rate is dropped rather than shown: gauges report at very different cadences,
# and a rate from hours ago is not evidence of what the water is doing now.
STORAGE_VERSION = 1
STORAGE_SAVE_DELAY = 30
MAX_RATE_AGE_HOURS = 6

TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_STEADY = "steady"
TREND_THRESHOLD_M = 0.01
