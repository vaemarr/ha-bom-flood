from __future__ import annotations

DOMAIN = "bom_flood"
PLATFORMS = ["sensor"]
DEFAULT_SCAN_INTERVAL = 15  # minutes

FTP_HOST = "ftp.bom.gov.au"
FTP_PATH = "/anon/gen/fwo/"
HCS_WATER_LEVEL_PREFIX = "IDQ65910"
HCS_RAINFALL_HOURLY_PREFIX = "IDQ65901"

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
ATTR_TIME_TO_MINOR = "time_to_minor_flood_hr"
ATTR_TIME_TO_MODERATE = "time_to_moderate_flood_hr"
ATTR_TIME_TO_MAJOR = "time_to_major_flood_hr"

FLOOD_STATUS_BELOW = "Below Flood Level"
FLOOD_STATUS_MINOR = "Minor"
FLOOD_STATUS_MODERATE = "Moderate"
FLOOD_STATUS_MAJOR = "Major"
FLOOD_STATUS_UNKNOWN = "Unknown"

TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_STEADY = "steady"
TREND_THRESHOLD_M = 0.01
