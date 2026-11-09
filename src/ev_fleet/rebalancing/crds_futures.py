"""Declared synthetic futures, not a learned or probability-calibrated forecast."""
from math import ceil
from ev_fleet.common.parking import CHARGE_ON_ARRIVAL_BELOW_PCT, CHARGING_TARGET_SOC_PCT

from ev_fleet.contracts.crds import CrdsFuture, CrdsParameters, CrdsZoneTrajectory
from .crds_routes import travel_minutes


MODEL_VERSION = "crds-futures-v6"


def parameters_for(settings):
    return CrdsParameters(horizon_minutes=settings.horizon_minutes,
                          slot_minutes=settings.slot_minutes,
                          minimum_gain_vehicle_minutes=settings.minimum_gain_vehicle_minutes,
                          priority_coverage_pct=20)


def build_futures(inputs, target_zone_id, settings, *, expand_age=True, min_reserve_pct=20):
    """Keep base profiles and append age profiles within one frozen input package."""
    if inputs.issues:
        raise ValueError("incomplete planning observations: " + ",".join(inputs.issues))
    params = parameters_for(settings)
    demands = {zone.zone_id: zone.demand for zone in inputs.demand.zones if zone.demand is not None}
    if target_zone_id not in demands or len(demands) != len(inputs.demand.zones):
        raise ValueError("missing target or demand")
    profiles = [("nominal", 0, 0, 0, False),
                ("demand_shift", settings.demand_step, 0, 0, False),
                ("late_returns", 0, settings.return_delay_minutes, 0, False),
                ("unavailable_returns", 0, 0, 0, True),
                ("delayed_inbound", 0, 0, settings.journey_delay_minutes, False),
                ("combined", settings.demand_step, settings.return_delay_minutes,
                 settings.journey_delay_minutes, True)]
    fixed_targets = set(inputs.demand.fixed_demand_zone_ids)
    dynamic_demand_age = max(0, max(
        ((inputs.evaluated_at - demand.observed_at).total_seconds()
         for zone_id, demand in demands.items() if zone_id not in fixed_targets), default=0))
    levels = min(settings.max_age_levels, int(dynamic_demand_age // settings.age_step_seconds)) if expand_age else 0
    for level in range(1, levels + 1):
        # Older demand only adds uncertainty about demand. Routes and returns
        # already have their own delay scenarios.
        profiles.append((f"age_{level}", settings.demand_step * level, 0, 0, False))
    futures = []
    vehicles = {v.vehicle_id: v for v in inputs.fleet.vehicles}
    for future_id, shift, return_delay, journey_delay, omit_returns in profiles:
        stock = {z: [inputs.available_by_zone[z]] * params.slot_count for z in demands}

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
            route = command.route
            if route is None or vehicle.telemetry is None:
                continue
            remaining = max(0, route.distance_km - dispatch.distance_travelled_km)
            arrival_soc = vehicle.telemetry.soc_pct - 100 * remaining * vehicle.consumption_kwh_per_km / vehicle.battery_capacity_kwh
            if arrival_soc < min_reserve_pct:
                continue
            minutes = travel_minutes(route, inputs.traffic, inputs.evaluated_at,
                                     distance_travelled_km=dispatch.distance_travelled_km,
                                     time_scale=settings.simulation_time_scale)
            if arrival_soc < CHARGE_ON_ARRIVAL_BELOW_PCT:
                site = next((s for s in inputs.facilities.facilities
                             if s.facility_id == dispatch.parking_facility_id), None)
                if site is None or site.charging_power_kw is None:
                    continue
                minutes += (max(0, CHARGING_TARGET_SOC_PCT - arrival_soc) / 100 * vehicle.battery_capacity_kwh
                            / site.charging_power_kw * 60 / settings.simulation_time_scale)
            credit(command.target_zone_id, minutes + journey_delay)
        if not omit_returns:
            for vehicle in vehicles.values():
                rental = vehicle.active_rental
                if (rental is None or vehicle.telemetry is None
                        or vehicle.telemetry.soc_pct < max(30, min_reserve_pct) or not rental.expected_return_zone_id):
                    continue
                minutes = max(params.slot_minutes,
                              (rental.expected_end_at - inputs.evaluated_at).total_seconds() / 60)
                credit(rental.expected_return_zone_id, minutes + return_delay)
        age_profile = future_id.startswith("age_")
        trajectories = {}
        for zone_id, demand in demands.items():
            desired = demand.desired_available_vehicles
            if not age_profile or zone_id not in fixed_targets:
                desired += -shift if zone_id == target_zone_id else shift
            trajectories[zone_id] = CrdsZoneTrajectory(
                desired_stock=[max(0, desired)] * params.slot_count, usable_stock=stock[zone_id])
        futures.append(CrdsFuture(future_id=future_id, zones=trajectories))
    return futures
