"""Constants for the CUPRA EU Data Act integration."""

from datetime import timedelta

DOMAIN = "cupra_dataact"
MANUFACTURERS = {"cupra": "CUPRA", "seat": "SEAT", "volkswagen": "Volkswagen", "audi": "Audi", "skoda": "Škoda"}
PORTAL_URL = "https://eu-data-act.drivesomethinggreater.com"

CONF_BRAND = "brand"
CONF_LOCAL_SOC = "local_soc_entity"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_BRAND = "cupra"
# the portal stores a dataset about every 15 minutes; polling the list is cheap
DEFAULT_SCAN_INTERVAL = timedelta(minutes=5)
MIN_SCAN_INTERVAL_MINUTES = 2

STORAGE_VERSION = 1

# Data point ids. GUID keys are unique; dataFieldName values may repeat across ECUs
# (see evcc vehicle/vw/eudataact/types.go and the portal data dictionary).
SOC = (
    "506cb83e-f99f-3af3-bbeb-0429b69a78d9",  # battery_state_report.soc (current, MEB)
    "battery_level_HV.value",
    "state_of_charge",
    "hv_soc",
)
RANGE = (
    "cruising_range_secondary_engine",
    "cruising_range_primary_engine",
    "cruising_range_combined",
    "0ca40e18-0564-3eda-bcc0-7aee9ef44f04",  # MEB cruising range
)
ODOMETER = (
    "69d437d3-7baa-38bc-b842-f5baf99ddade",  # mileage.value (current)
    "mileage",
    "mileage.value",
)
TARGET_SOC = ("settings.target_soc",)
REMAINING_CHARGE_TIME = ("remaining_charging_time",)
CHARGE_POWER = ("charge_power",)
CHARGING_STATE = ("charging_state", "charging_state_report.current_charge_state")
CHARGING_SCENARIO = ("charging_state_report.charging_scenario",)
PLUG_STATE = ("plug_state", "charging_plug1_connectionstate")
