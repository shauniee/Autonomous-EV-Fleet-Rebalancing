"""Shared mock charging threshold for batch planning and approval."""
CHARGE_ON_ARRIVAL_BELOW_PCT = 35.0
# Search radius from facility geographic centre to a OneMap drivable endpoint.
# This is NOT the 50 m physical-arrival tolerance.
MAX_FACILITY_ROAD_ACCESS_KM = .15
CHARGING_TARGET_SOC_PCT = 80.0
# Explicit simulation assumption when an operator does not publish charger power.
DEFAULT_CHARGING_POWER_KW = 7.4
