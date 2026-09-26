import asyncio
from uuid import uuid4
import pytest
from ev_fleet.contracts.demand import ZoneState
from ev_fleet.contracts.vehicles import VehicleState
from ev_fleet.demand.aggregation import assigned_zone
from ev_fleet.common.travel import distance_km
from tests.phase3a_support import DemandCluster


def zones(view):
    return {zone["zone_id"]: zone for zone in view["zones"]}


def test_initial_coordinate_fallback_and_unassigned_low_soc(clock):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            assert (await cluster.fleet.get("ev-1"))["current_zone_id"] is None
            view = await cluster.heatmap()
            assert zones(view)["bishan"]["available_evs"] == 1
            assert (await cluster.fleet.get("ev-1"))["current_zone_id"] is None  # Read-only composition.
            await cluster.telemetry(soc=19)
            view = await cluster.heatmap()
            assert zones(view)["bishan"]["available_evs"] == 0 and view["excluded_low_soc_evs"] == 1
            await cluster.telemetry(position={"latitude": 1.16, "longitude": 103.61})
            view = await cluster.heatmap()
            assert view["unassigned_evs"] == 1
            assert sum(zone["available_evs"] for zone in view["zones"]) == 0
    asyncio.run(scenario())


def test_zone_radius_boundary_and_tie(clock):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            vehicle = VehicleState.model_validate(await cluster.fleet.get("ev-1"))
            zone = ZoneState.model_validate(await cluster.demand.get("bishan"))
            radius = distance_km(vehicle.telemetry.position.model_dump(), zone.position.model_dump())
            assert assigned_zone(vehicle, {"bishan": zone}, radius) == "bishan"
            assert assigned_zone(vehicle, {"bishan": zone}, radius - 1e-6) is None
            twin = zone.model_copy(update={"zone_id": "a"})
            assert assigned_zone(vehicle, {"bishan": zone, "a": twin}, 2) == "a"
            vehicle.current_zone_id = "bishan"
            assert assigned_zone(vehicle, {"bishan": zone, "a": twin}, 2) == "bishan"
    asyncio.run(scenario())


def test_claims_move_from_pending_to_projected_to_available(clock):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            await cluster.set_demand(value=3)
            dispatch, _ = await cluster.approve()
            key = dispatch["dispatch_id"]
            view = zones(await cluster.heatmap())
            assert view["bishan"]["available_evs"] == 0
            assert view["cbd"]["inbound_pending_evs"] == 1
            assert view["cbd"]["projected_shortage"] == 3
            await cluster.telemetry(state="RELOCATING", dispatch_id=key)
            view = zones(await cluster.heatmap())
            assert view["cbd"]["inbound_relocating_evs"] == 1
            assert view["cbd"]["current_shortage"] == 3 and view["cbd"]["projected_shortage"] == 2
            assert sum(zone["available_evs"] + zone["inbound_relocating_evs"] for zone in view.values()) == 1
            await cluster.telemetry(state="ARRIVED", dispatch_id=key, position=dispatch["command"]["target_position"])
            view = zones(await cluster.heatmap())
            assert view["cbd"]["arrived_pending_evs"] == 1 and view["cbd"]["available_evs"] == 0
            assert (await cluster.dispatch.get(key))["state"] == "SUCCEEDED"
            await cluster.telemetry(position=dispatch["command"]["target_position"])
            view = zones(await cluster.heatmap())
            assert view["cbd"]["available_evs"] == 1 and view["cbd"]["inbound_relocating_evs"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["stale", "offline", "mismatch", "failed"])
def test_unconfirmed_inbound_is_not_projected_supply(clock, kind):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            await cluster.set_demand(value=3)
            dispatch, _ = await cluster.approve()
            await cluster.telemetry(state="FAILED" if kind == "failed" else "RELOCATING",
                dispatch_id=uuid4() if kind == "mismatch" else dispatch["dispatch_id"])
            if kind in ("stale", "offline"):
                clock.advance(15 if kind == "stale" else 30)
            view = zones(await cluster.heatmap())["cbd"]
            assert view["inbound_relocating_evs"] == 0 and view["inbound_unconfirmed_evs"] == 1
            assert view["projected_shortage"] == 3 and view["data_quality"] == "DEGRADED"
    asyncio.run(scenario())


def test_old_fleet_epoch_claim_does_not_count_as_inbound(clock):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            await cluster.set_demand(value=3)
            dispatch, _ = await cluster.approve()
            await cluster.telemetry(state="RELOCATING", dispatch_id=dispatch["dispatch_id"])
            # Fault injection: a structurally valid claim references another Fleet epoch.
            cluster.fleet.vehicles["ev-1"]["active_claim"]["fleet_instance_id"] = str(uuid4())
            cbd = zones(await cluster.heatmap())["cbd"]
            assert cbd["inbound_relocating_evs"] == 0 and cbd["inbound_unconfirmed_evs"] == 1
    asyncio.run(scenario())
