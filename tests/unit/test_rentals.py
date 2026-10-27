import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from ev_fleet.contracts.dispatch import ClaimRequest
from ev_fleet.contracts.rentals import RentalStart, RentalReturn
from ev_fleet.fleet.app import create_app
from tests.phase2_support import Cluster


def test_rental_lifecycle_excludes_ev_until_fresh_confirmed_free_floating_return(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle(soc=83)
            vehicle = await cluster.fleet.get("ev-1")
            rental_id = uuid4()
            begin = RentalStart(rental_id=rental_id, request_id=uuid4(),
                                expected_version=vehicle["state_version"],
                                expected_end_at=clock.now() + timedelta(hours=2))
            rented = await cluster.fleet.start_rental("ev-1", begin)
            assert rented["status"] == "RENTED"
            assert rented == await cluster.fleet.start_rental("ev-1", begin)
            assert (await cluster.fleet.get("ev-1"))["effective_availability"] == "UNAVAILABLE"
            rec = await cluster.recommend()
            assert rec["selected_vehicle_id"] is None
            assert any("RENTED" in item["exclusion_reasons"] for item in rec["excluded_candidates"])
            # A customer can return at any simulated GPS point, not necessarily a zone centre.
            actual = {"latitude": 1.4041, "longitude": 103.9045}
            clock.advance(3)
            await cluster.telemetry(position=actual, soc=68)
            current = await cluster.fleet.get("ev-1")
            assert current["effective_availability"] == "UNAVAILABLE"
            finish = RentalReturn(request_id=uuid4(), expected_version=current["state_version"])
            returned = await cluster.fleet.return_rental("ev-1", rental_id, finish)
            assert returned["status"] == "RETURNED"
            assert returned["return_position"] == actual
            assert returned["return_soc_pct"] == 68
            assert returned == await cluster.fleet.return_rental("ev-1", rental_id, finish)
            assert returned == await cluster.fleet.get_rental("ev-1", rental_id)
            available = await cluster.fleet.get("ev-1")
            assert available["active_rental"] is None
            assert available["current_zone_id"] is None
            assert available["effective_availability"] == "AVAILABLE"
            next_rec = await cluster.recommend()
            assert next_rec["selected_vehicle_id"] == "ev-1"
    asyncio.run(scenario())


def test_return_requires_new_fresh_telemetry_and_rejects_conflicting_retries(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            before = await cluster.fleet.get("ev-1")
            begin = RentalStart(rental_id=uuid4(), request_id=uuid4(),
                                expected_version=before["state_version"],
                                expected_end_at=clock.now() + timedelta(hours=1))
            await cluster.fleet.start_rental("ev-1", begin)
            current = await cluster.fleet.get("ev-1")
            first_return = RentalReturn(request_id=uuid4(), expected_version=current["state_version"])
            with pytest.raises(HTTPException) as not_returned:
                await cluster.fleet.return_rental("ev-1", begin.rental_id, first_return)
            assert not_returned.value.detail == "RETURN_TELEMETRY_REQUIRED"
            clock.advance(16)
            with pytest.raises(HTTPException) as stale:
                await cluster.fleet.return_rental("ev-1", begin.rental_id, first_return)
            assert stale.value.detail == "RETURN_TELEMETRY_STALE"
            await cluster.telemetry(position={"latitude": 1.2831, "longitude": 103.855}, soc=60)
            current = await cluster.fleet.get("ev-1")
            with pytest.raises(HTTPException) as version:
                await cluster.fleet.return_rental("ev-1", begin.rental_id, first_return)
            assert version.value.detail == "VEHICLE_VERSION_CONFLICT"
            body = RentalReturn(request_id=uuid4(), expected_version=current["state_version"])
            await cluster.fleet.return_rental("ev-1", begin.rental_id, body)
            with pytest.raises(HTTPException) as conflict:
                await cluster.fleet.return_rental("ev-1", begin.rental_id,
                                                  RentalReturn(request_id=uuid4(), expected_version=body.expected_version))
            assert conflict.value.detail == "RENTAL_RETURN_CONFLICT"
    asyncio.run(scenario())


def test_rental_start_and_dispatch_claim_are_mutually_exclusive(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            vehicle = await cluster.fleet.get("ev-1")
            rental = RentalStart(rental_id=uuid4(), request_id=uuid4(),
                                 expected_version=vehicle["state_version"],
                                 expected_end_at=clock.now() + timedelta(hours=1))
            claim = ClaimRequest(command_id=uuid4(), expected_instance_id=cluster.fleet.instance_id,
                                 expected_version=vehicle["state_version"], target_zone_id="cbd",
                                 target_position={"latitude": 1.283, "longitude": 103.8513},
                                 approve_before=clock.now() + timedelta(seconds=10),
                                 min_reserve_pct=20, route_factor=1.3, energy_margin_factor=1.15)
            result = await asyncio.gather(cluster.fleet.start_rental("ev-1", rental),
                                          cluster.fleet.claim("ev-1", uuid4(), claim),
                                          return_exceptions=True)
            assert sum(isinstance(value, HTTPException) for value in result) == 1
            assert sum(isinstance(value, dict) for value in result) == 1
            state = await cluster.fleet.get("ev-1")
            assert not (state["active_rental"] and state["active_claim"])
    asyncio.run(scenario())


def test_rejected_relocation_telemetry_while_rented_does_not_mutate_fleet(clock):
    async def scenario():
        from ev_fleet.contracts.vehicles import TelemetryUpdate
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            state = await cluster.fleet.get("ev-1")
            begin = RentalStart(rental_id=uuid4(), request_id=uuid4(),
                                expected_version=state["state_version"],
                                expected_end_at=clock.now() + timedelta(hours=2))
            await cluster.fleet.start_rental("ev-1", begin)
            before = await cluster.fleet.get("ev-1")
            rejected = TelemetryUpdate(
                session_id=cluster.sessions["ev-1"],
                sequence=cluster.sequences["ev-1"] + 1, observed_at=clock.now(),
                position={"latitude": 1.405, "longitude": 103.902}, soc_pct=40,
                availability="UNAVAILABLE", relocation_state="RELOCATING",
                active_dispatch_id=uuid4())
            with pytest.raises(HTTPException) as exc:
                await cluster.fleet.telemetry("ev-1", rejected)
            assert exc.value.detail == "RENTED_RELOCATION_CONFLICT"
            after = await cluster.fleet.get("ev-1")
            assert after["state_version"] == before["state_version"]
            assert after["telemetry"] == before["telemetry"]
            assert after["active_rental"] == before["active_rental"]
    asyncio.run(scenario())


def test_rental_start_request_id_cannot_be_reused_for_different_rental(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            state = await cluster.fleet.get("ev-1")
            rid = uuid4()
            request_id = uuid4()
            first = RentalStart(rental_id=rid, request_id=request_id,
                                expected_version=state["state_version"],
                                expected_end_at=clock.now() + timedelta(hours=2))
            receipt = await cluster.fleet.start_rental("ev-1", first)
            assert await cluster.fleet.start_rental("ev-1", first) == receipt
            second = RentalStart(rental_id=uuid4(), request_id=request_id,
                                 expected_version=(await cluster.fleet.get("ev-1"))["state_version"],
                                 expected_end_at=first.expected_end_at)
            with pytest.raises(HTTPException) as conflict:
                await cluster.fleet.start_rental("ev-1", second)
            assert conflict.value.detail == "RENTAL_REQUEST_ID_CONFLICT"
    asyncio.run(scenario())


def test_rental_http_api_and_response_contract(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cluster.fleet)),
                                         base_url="http://test") as client:
                state = (await client.get("/api/v1/vehicles/ev-1")).json()
                rid, request_id = uuid4(), uuid4()
                start = {"rental_id": str(rid), "request_id": str(request_id),
                         "expected_version": state["state_version"],
                         "expected_end_at": (clock.now() + timedelta(hours=2)).isoformat()}
                result = await client.post("/api/v1/vehicles/ev-1/rentals", json=start)
                assert result.status_code == 201, result.text
                assert result.json()["status"] == "RENTED"
                assert (await client.get("/api/v1/vehicles/ev-1")).json()["active_rental"]["rental_id"] == str(rid)
                clock.advance(1)
                await cluster.telemetry(position={"latitude": 1.3902, "longitude": 103.902}, soc=65)
                current = (await client.get("/api/v1/vehicles/ev-1")).json()
                returned = await client.post(f"/api/v1/vehicles/ev-1/rentals/{rid}/return",
                                             json={"request_id": str(uuid4()), "expected_version": current["state_version"]})
                assert returned.status_code == 200, returned.text
                assert returned.json()["return_soc_pct"] == 65
                assert (await client.get(f"/api/v1/vehicles/ev-1/rentals/{rid}")).json() == returned.json()
    asyncio.run(scenario())
