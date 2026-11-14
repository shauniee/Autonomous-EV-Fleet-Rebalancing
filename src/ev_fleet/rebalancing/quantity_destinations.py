"""Capacity-aware destination matching for explicit operator quantity requests."""
from collections import defaultdict
from typing import Any

from ev_fleet.common.parking import CHARGE_ON_ARRIVAL_BELOW_PCT
from ev_fleet.contracts.facilities import Facility
from ev_fleet.contracts.vehicles import VehicleState

Candidate = tuple[VehicleState, Facility, dict[str, Any]]
Slot = tuple[str, str]


def assign_destinations(pools: dict[str, list[Candidate]]) -> list[Candidate]:
    """Find a maximum matching without taking holds or changing facility state.

    Distinguish ordinary bays and charger bays so a flexible EV cannot prevent
    an EV requiring charging from using its only compatible destination.
    """
    capacities: dict[Slot, int] = {}
    edges: dict[str, list[tuple[Slot, Candidate]]] = {}
    occupants: dict[Slot, list[str]] = defaultdict(list)
    assigned: dict[str, Candidate] = {}

    for identifier, choices in pools.items():
        options: list[tuple[Slot, Candidate]] = []
        for candidate in choices:
            _, site, assessment = candidate
            regular = (site.facility_id, "PARKING")
            charging = (site.facility_id, "CHARGING")
            capacities[regular] = site.parking_available - site.charging_available
            capacities[charging] = site.charging_available
            if assessment["predicted_arrival_soc_pct"] >= CHARGE_ON_ARRIVAL_BELOW_PCT:
                if capacities[regular] > 0:
                    options.append((regular, candidate))
            if capacities[charging] > 0:
                options.append((charging, candidate))
        edges[identifier] = options

    def place(identifier: str, visited: set[str]) -> bool:
        if identifier in visited:
            return False
        visited.add(identifier)
        for slot, candidate in edges[identifier]:
            owners = occupants[slot]
            if len(owners) < capacities[slot]:
                owners.append(identifier)
                assigned[identifier] = candidate
                return True
            for index, previous in enumerate(owners):
                if place(previous, visited):
                    owners[index] = identifier
                    assigned[identifier] = candidate
                    return True
        return False

    # Place constrained EVs first, with deterministic nearest-site preferences.
    for identifier in sorted(pools, key=lambda key: (len(edges[key]), key)):
        place(identifier, set())
    return [assigned[key] for key in sorted(assigned)]
