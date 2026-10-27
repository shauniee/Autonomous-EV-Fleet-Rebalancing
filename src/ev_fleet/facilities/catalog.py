"""Deterministic fictional facilities near the six existing simulated zone centres.

All names, coordinates, parking counts and charging counts below are MOCK VALUES.
They are not real carpark sites, availability observations or bookable EV bays.
Charging bays form a subset of parking bays, not additional parking capacity.
"""
from ev_fleet.contracts.facilities import Facility
from ev_fleet.demand.store import SINGAPORE_ZONES

# zone, location offset (latitude, longitude), parking total/available,
# charging bays/available. A closed facility is included for UI testing.
SEEDS = (
    ("jurong-east", 0.0020, -0.0014, 70, 16, 6, 2, "OPEN"),
    ("jurong-east", -0.0017, 0.0030, 44, 8, 0, 0, "OPEN"),
    ("jurong-east", 0.0034, 0.0015, 32, 5, 4, 1, "OPEN"),
    ("woodlands", 0.0022, 0.0008, 52, 12, 4, 2, "OPEN"),
    ("woodlands", -0.0021, -0.0022, 35, 6, 0, 0, "OPEN"),
    ("bishan", 0.0016, -0.0020, 66, 14, 6, 3, "OPEN"),
    ("bishan", -0.0014, 0.0024, 30, 8, 0, 0, "OPEN"),
    ("punggol", 0.0017, 0.0019, 58, 9, 8, 3, "OPEN"),
    ("punggol", -0.0027, -0.0016, 36, 7, 0, 0, "OPEN"),
    ("tampines", 0.0014, -0.0021, 76, 18, 6, 4, "OPEN"),
    ("tampines", -0.0024, 0.0013, 48, 11, 4, 2, "OPEN"),
    ("tampines", 0.0027, 0.0034, 28, 0, 2, 0, "CLOSED"),
    ("cbd", 0.0012, 0.0019, 90, 12, 12, 6, "OPEN"),
    ("cbd", -0.0022, -0.0020, 50, 5, 4, 1, "OPEN"),
    ("cbd", 0.0030, -0.0017, 42, 3, 0, 0, "OPEN"),
)
ZONES = {zone_id: (name, lat, lng) for zone_id, name, lat, lng in SINGAPORE_ZONES}


def build_catalogue():
    per_zone = {}
    facilities = []
    for zone_id, lat_shift, lng_shift, total, available, chargers, free_chargers, status in SEEDS:
        index = per_zone.get(zone_id, 0) + 1
        per_zone[zone_id] = index
        name, lat, lng = ZONES[zone_id]
        facilities.append(Facility(
            facility_id=f"mock-{zone_id}-{index:02d}",
            name=f"{name} simulated parking {index}",
            zone_id=zone_id,
            position={"latitude": round(lat + lat_shift, 6), "longitude": round(lng + lng_shift, 6)},
            parking_capacity=total, parking_available=available,
            charging_bays=chargers, charging_available=free_chargers,
            connector_types=["CCS2"] if chargers else [],
            status=status, source="MOCK",
        ))
    return tuple(facilities)


CATALOGUE = build_catalogue()
BY_ID = {facility.facility_id: facility for facility in CATALOGUE}
