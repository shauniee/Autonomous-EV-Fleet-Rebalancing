"""Declared synthetic futures, not a learned or probability-calibrated forecast."""
from math import ceil

from ev_fleet.contracts.crds import CrdsFuture, CrdsParameters, CrdsZoneTrajectory
from .crds_routes import travel_minutes


MODEL_VERSION = "crds-futures-v1"


def parameters_for(settings):
    return CrdsParameters(horizon_minutes=settings.horizon_minutes,
                          slot_minutes=settings.slot_minutes,
                          minimum_gain_vehicle_minutes=settings.minimum_gain_vehicle_minutes)


def observation_age(inputs):
    times = [zone.demand.observed_at for zone in inputs.demand.zones if zone.demand]
    times.extend(v.telemetry.observed_at for v in inputs.fleet.vehicles if v.telemetry)
    return max((max(0, (inputs.evaluated_at - t).total_seconds()) for t in times), default=0)


def build_futures(inputs, target_zone_id, settings, *, expand_age=True):
    """Keep base profiles and append age profiles within one frozen input package."""
    if inputs.issues:
        raise ValueError("incomplete planning observations: " + ",".join(inputs.issues))
    params = parameters_for(settings)
    zones = {z.zone_id: z for z in inputs.demand.zones}
    if target_zone_id not in zones or any(z.demand is None for z in zones.values()):
        raise ValueError("missing target or demand")
    profiles = [("nominal", 0, 0, 0, False, False),
                ("demand_shift", settings.demand_step, 0, 0, False, False),
                ("late_returns", 0, settings.return_delay_minutes, 0, False, False),
                ("unavailable_returns", 0, 0, 0, True, False),
                ("delayed_inbound", 0, 0, settings.journey_delay_minutes, False, True),
                ("combined", settings.demand_step, settings.return_delay_minutes,
                 settings.journey_delay_minutes, True, True)]
    levels = min(settings.max_age_levels, int(observation_age(inputs) // settings.age_step_seconds)) if expand_age else 0
    for level in range(1, levels + 1):
        profiles.append((f"age_{level}", settings.demand_step * level,
                         settings.return_delay_minutes * level,
                         settings.journey_delay_minutes * level, True, True))
    futures = []
    vehicles = {v.vehicle_id: v for v in inputs.fleet.vehicles}
    for future_id, shift, return_delay, journey_delay, omit_returns, omit_uncertain in profiles:
        stock = {z: [inputs.available_by_zone[z]] * params.slot_count for z in zones}

        def credit(zone_id, minutes):
            if zone_id not in stock:
                raise ValueError("incoming vehicle has unknown destination")
            first = min(params.slot_count, ceil(max(params.slot_minutes, minutes) / params.slot_minutes))
            for slot in range(first, params.slot_count):
                stock[zone_id][slot] += 1

        for dispatch in inputs.planning.dispatches:
            if dispatch.command is None or dispatch.vehicle_id not in vehicles:
                raise ValueError("unresolved dispatch identity")
            vehicle = vehicles[dispatch.vehicle_id]
            command = dispatch.command
            if not vehicle.active_claim:
                origin = inputs.vehicle_zones.get(vehicle.vehicle_id)
                if origin not in stock or min(stock[origin]) < 1:
                    raise ValueError("pending intent stock is inconsistent")
                stock[origin] = [n - 1 for n in stock[origin]]
            if dispatch.state == "UNKNOWN" and omit_uncertain:
                continue
            route = command.route
            if route is None or vehicle.telemetry is None:
                continue
            if vehicle.telemetry.soc_pct < 20:
                continue
            minutes = travel_minutes(route, inputs.traffic, inputs.evaluated_at,
                                     distance_travelled_km=dispatch.distance_travelled_km)
            credit(command.target_zone_id, minutes + journey_delay)
        if not omit_returns:
            for vehicle in vehicles.values():
                rental = vehicle.active_rental
                if (rental is None or vehicle.telemetry is None
                        or vehicle.telemetry.soc_pct < 30 or not rental.expected_return_zone_id):
                    continue
                minutes = max(params.slot_minutes,
                              (rental.expected_end_at - inputs.evaluated_at).total_seconds() / 60)
                credit(rental.expected_return_zone_id, minutes + return_delay)
        futures.append(CrdsFuture(future_id=future_id, zones={
            z: CrdsZoneTrajectory(
                desired_stock=[max(0, zones[z].demand.desired_available_vehicles +
                                   (-shift if z == target_zone_id else shift))] * params.slot_count,
                usable_stock=stock[z]) for z in zones}))
    return futures
