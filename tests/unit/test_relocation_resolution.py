"""Closure uses the real Dispatch and Fleet handlers over an isolated transport."""
import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from ev_fleet.common.settings import SimulatorRecoverySettings
from ev_fleet.contracts.dispatch import BatchApproval
from ev_fleet.contracts.recovery import (SimulatorCheckpoint, SimulatorCheckpointRequest,
    SimulatorConfiguration, SimulatorIdleRestoreRequest, SimulatorSessionRequest)
from ev_fleet.contracts.vehicles import TelemetryUpdate, VehicleRegistration
from ev_fleet.dispatch.resolution import advance_resolution
from ev_fleet.fleet.store import FleetStore
from ev_fleet.simulator.movement import Movement
from tests.dispatch_support import Cluster
from tests.unit.test_idle_simulator_recovery import CheckpointRepository
from tests.unit.test_persistence import MemoryRepository


@pytest.mark.parametrize("stopped", ["expired_mid_route", "failed_live", "unstarted_after_charge"])
def test_stop_fences_owner_and_reconciles_after_lost_fleet_receipt(clock, monkeypatch, stopped):
    monkeypatch.setenv("CRDS_MODE", "DISABLED")
    client_type = httpx.AsyncClient

    async def scenario():
        cluster = Cluster(clock)
        recovery = SimulatorRecoverySettings(enabled=True, vehicle_ids=["ev-1"],
            dispatch_url="http://dispatch.test")
        cluster.fleet = FleetStore(cluster.config, clock, repository=CheckpointRepository(),
                                   recovery_settings=recovery)
        async with cluster:
            monkeypatch.setattr("ev_fleet.fleet.recovery.httpx.AsyncClient",
                lambda **kwargs: client_type(transport=httpx.MockTransport(cluster.route), **kwargs))
            await cluster.fleet.start()
            service = cluster.dispatch
            service.repository = MemoryRepository()
            fleet = cluster.fleet
            await fleet.register(VehicleRegistration(vehicle_id="ev-1", battery_capacity_kwh=60,
                                                      consumption_kwh_per_km=.18))
            lease = await fleet.simulator_recovery.session("ev-1", SimulatorSessionRequest(
                request_id=uuid4(), action="ACQUIRE", expected_fleet_instance_id=fleet.instance_id,
                expected_control_version=1, expected_generation=0, expected_owner_revision=0,
                expected_checkpoint_revision=0))
            config = SimulatorConfiguration(capacity_kwh=60, consumption_kwh_per_km=.18,
                                             speed_kph=30, time_scale=10)
            physical = TelemetryUpdate(session_id=lease.session_id, sequence=0, observed_at=clock.now(),
                position={"latitude": 1.35, "longitude": 103.8513}, soc_pct=80,
                speed_kph=0, odometer_km=2, heading_deg=0, availability="AVAILABLE")
            idle = SimulatorCheckpoint(vehicle_id="ev-1", fleet_instance_id=fleet.instance_id,
                                       simulation=config, physical=physical)
            await fleet.simulator_recovery.checkpoint("ev-1", SimulatorCheckpointRequest(
                request_id=uuid4(), session_id=lease.session_id, generation=1,
                expected_checkpoint_revision=0, checkpoint=idle, publish_telemetry=True))
            unstarted = stopped == "unstarted_after_charge"
            if unstarted:
                # Seed the previously persisted idle state of an EV that finished charging.
                physical = TelemetryUpdate.model_validate({
                    **physical.model_dump(), "charging_state": "COMPLETE",
                    "parked_facility_id": "origin-charger", "charging_power_kw": 7.4,
                })
                idle = idle.model_copy(update={"physical": physical, "completed_command_id": uuid4()})
                fleet_record = fleet.record("ev-1")
                fleet_record["telemetry"] = physical
                fleet_record["simulator_checkpoint"]["data"] = idle.model_dump(mode="json")
            batch = await cluster.client.post("http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 1})
            approved = await service.approve_batch(BatchApproval(request_id=uuid4(),
                batch_recommendation_id=batch.json()["batch_recommendation_id"]))
            dispatch_id = approved["items"][0]["dispatch_id"]
            record = service.store.get(dispatch_id)
            moving = idle
            if not unstarted:
                command = (await service.commands("ev-1"))["commands"][0]

                movement = Movement.from_checkpoint(idle, config)
                assert movement.accept(command)
                movement.start()
                movement.advance(12, 30)
                if stopped == "failed_live":
                    # Inject a stopped execution state at a valid route location.
                    movement.state, movement.speed_kph, movement.reason = "FAILED", 0, "SIMULATED_FAILURE"
                saved = movement.snapshot()
                moving = SimulatorCheckpoint(vehicle_id="ev-1", fleet_instance_id=fleet.instance_id,
                    simulation=config, physical=TelemetryUpdate.model_validate({
                        **physical.model_dump(mode="json"), **saved.pop("physical"), "sequence": 1}), **saved)
                await fleet.simulator_recovery.checkpoint("ev-1", SimulatorCheckpointRequest(
                    request_id=uuid4(), session_id=lease.session_id, generation=1,
                    expected_checkpoint_revision=1, checkpoint=moving, publish_telemetry=True))
            path = f"http://dispatch.test/api/v1/dispatches/{dispatch_id}/resolution"
            if stopped != "failed_live":
                assert not (await cluster.client.get(path)).json()["can_end"]
                clock.advance(31)
            assessment = (await cluster.client.get(path)).json()
            assert assessment["can_end"], assessment
            assert assessment["unstarted"] is unstarted
            if unstarted:
                assert record.data["delivery_attempts"] == 0
                recovery = (await cluster.client.get(path.removesuffix("/resolution") + "/recovery")).json()
                assert not recovery["can_resume"]
                assert recovery["blockers"][0]["detail"] == "CHECKPOINT_COMMAND_MISMATCH"
            request = {"request_id": str(uuid4()), "reason": "Recover the remaining fleet",
                       "expected_assessment_revision": assessment["assessment_revision"]}
            stale = await cluster.client.post(path, json=request | {"expected_assessment_revision": "stale"})
            assert stale.status_code == 409
            requested = await cluster.client.post(path, json=request)
            assert requested.status_code == 202, requested.text
            assert requested.json()["status"] == "REQUESTED"
            assert not (await service.commands("ev-1"))["commands"]

            cluster.failures.append({"method": "POST", "path": "/simulator-stop", "action": "lose_response"})
            await advance_resolution(service, record)
            assert record.resolution["status"] == "REQUESTED"
            assert next(iter(cluster.facilities.holds.values()))["status"] == "RESERVED"
            assert (await fleet.get("ev-1"))["active_claim"] is None
            await service.start()  # Reload the persisted intent after the lost response.
            record = service.store.get(dispatch_id)
            cluster.failures.append({"method": "POST", "path": f"/holds/{dispatch_id}/release", "action": "offline"})
            await advance_resolution(service, record)
            assert record.resolution["status"] == "CLEANUP_PENDING"
            assert next(iter(cluster.facilities.holds.values()))["status"] == "RESERVED"
            reconciled = await cluster.client.post(path.removesuffix("/resolution") + "/reconcile")
            assert reconciled.json()["parking_status"] == "RELEASED"
            await advance_resolution(service, record)
            assert record.resolution["status"] == "ENDED"
            assert (await cluster.client.post(path, json=request)).json()["status"] == "ENDED"
            assert (await cluster.client.post(path, json=request | {"reason": "Changed"})).status_code == 409
            assert record.data["failure_reason"] == "OPERATOR_ENDED_RELOCATION"
            assert next(iter(cluster.facilities.holds.values()))["status"] == "RELEASED"
            after = await fleet.simulator_recovery.snapshot("ev-1")
            assert after.checkpoint.command is None
            assert after.checkpoint.physical.position == moving.physical.position
            assert after.checkpoint.physical.soc_pct == moving.physical.soc_pct
            with pytest.raises(HTTPException, match="SIMULATOR_LEASE_EXPIRED"):
                await fleet.simulator_recovery.checkpoint("ev-1", SimulatorCheckpointRequest(
                    request_id=uuid4(), session_id=lease.session_id, generation=1,
                    expected_checkpoint_revision=after.checkpoint_revision, checkpoint=moving))
            if unstarted:
                assert after.checkpoint.physical.charging_state == "COMPLETE"
                assert after.checkpoint.physical.parked_facility_id == "origin-charger"
                assert (await fleet.get("ev-1"))["effective_availability"] == "UNAVAILABLE"
                return
            owner = (await fleet.get("ev-1"))["simulator_recovery"]
            idle_session = await fleet.simulator_recovery.session("ev-1", SimulatorIdleRestoreRequest(
                request_id=uuid4(), expected_fleet_instance_id=fleet.instance_id,
                expected_control_version=after.control_version, expected_generation=after.generation,
                expected_owner_revision=owner["owner_revision"],
                expected_checkpoint_revision=after.checkpoint_revision, simulation=config))
            assert idle_session.generation == 2
            restored = idle_session.checkpoint
            sample = TelemetryUpdate.model_validate({
                **restored.physical.model_dump(), "sequence": 1, "observed_at": clock.now(),
                "availability": "AVAILABLE",
            })
            await fleet.simulator_recovery.checkpoint("ev-1", SimulatorCheckpointRequest(
                request_id=uuid4(), session_id=idle_session.session_id, generation=2,
                expected_checkpoint_revision=idle_session.checkpoint_revision,
                checkpoint=restored.model_copy(update={"physical": sample}), publish_telemetry=True))
            vehicle = await fleet.get("ev-1")
            assert not vehicle["stale"]
            assert vehicle["effective_availability"] == "UNAVAILABLE"

    asyncio.run(scenario())
