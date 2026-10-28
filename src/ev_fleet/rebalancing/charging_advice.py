"""Read-only, safety-checked charger advice for low-SOC vehicles.

The matching is advisory: it does not claim EVs, hold bays, create charging
tasks or dispatch. Approval goes through the existing batch/dispatch workflow.
"""
from ev_fleet.rebalancing.policy import assess

LOW_SOC_ADVICE_PCT = 25.0


def build_charging_advice(fleet, facilities, settings, now):
    advice = []
    for vehicle in fleet["vehicles"]:
        telemetry = vehicle["telemetry"]
        if telemetry is None or telemetry["soc_pct"] > LOW_SOC_ADVICE_PCT:
            continue

        current_soc = telemetry["soc_pct"]
        # assess() applies the SAME freshness, session, rental, task, dispatch
        # and minimum-arrival-reserve checks used by batch dispatch decisions.
        eligibility = assess(vehicle, {
            "position": telemetry["position"],
            "charging": {"observed_at": now.isoformat(), "available_slots": 1},
        }, settings, now)
        if not eligibility["eligible"]:
            advice.append({
                "vehicle_id": vehicle["vehicle_id"], "current_soc_pct": current_soc,
                "status": "INELIGIBLE", "reasons": eligibility["exclusion_reasons"],
                "charger": None,
            })
            continue

        options = []
        for site in facilities:
            if (site.status != "OPEN" or not site.is_fresh(now)
                    or site.parking_available < 1 or site.charging_available < 1):
                continue
            target = {
                "position": site.position.model_dump(mode="json"),
                "charging": {"observed_at": now.isoformat(),
                             "available_slots": site.charging_available},
            }
            check = assess(vehicle, target, settings, now)
            if not check["eligible"]:
                continue
            options.append({
                "facility_id": site.facility_id, "facility_name": site.name,
                "zone_id": site.zone_id, "source": site.source,
                "distance_km": check["distance_km"],
                "energy_required_kwh": check["energy_required_kwh"],
                "predicted_arrival_soc_pct": check["predicted_arrival_soc_pct"],
                "charging_available": site.charging_available,
                "charging_power_kw": site.charging_power_kw,
            })

        # Nearest safe simulated driving distance. Tie-breaks are stable.
        options.sort(key=lambda option: (
            option["distance_km"], -option["predicted_arrival_soc_pct"],
            option["facility_id"]))
        selected = options[0] if options else None
        advice.append({
            "vehicle_id": vehicle["vehicle_id"], "current_soc_pct": current_soc,
            "status": "RECOMMENDED" if selected else "NO_SAFE_CHARGER",
            "reasons": [] if selected else ["NO_OPEN_REACHABLE_CHARGER_WITH_CAPACITY"],
            "charger": selected,
        })

    return {
        "generated_at": now.isoformat(),
        "trigger_soc_pct": LOW_SOC_ADVICE_PCT,
        "min_arrival_reserve_pct": settings.min_reserve_pct,
        "vehicles": sorted(advice, key=lambda row: (row["current_soc_pct"], row["vehicle_id"])),
        "assumptions": [
            "Advisory only; no EV movement or charger reservation",
            "Straight-line distance adjusted by configured route and energy margins, not confirmed road routing",
            "Facility availability is simulated and must be revalidated before operator approval",
            "Does not create ground-staff charging tasks or command real charging hardware",
        ],
    }
