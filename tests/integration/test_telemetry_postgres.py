"""Real PostgreSQL integration: latest state plus append-only unique history.

Run with --telemetry-postgres and a disposable TELEMETRY_TEST_DATABASE_URL.
"""
import asyncio
from uuid import uuid4

from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.common import utcnow
from ev_fleet.contracts.vehicles import TelemetryUpdate, VehicleRegistration
from ev_fleet.fleet.store import FleetStore


def test_history_is_durable_atomic_and_unique_between_sessions(telemetry_database_url):
    async def scenario():
        database = telemetry_database_url
        vehicle_id = f"ev-test-{uuid4().hex}"
        store = FleetStore(repository=SnapshotRepository("fleet", database))
        await store.start()
        try:
            await store.register(VehicleRegistration(vehicle_id=vehicle_id,
                battery_capacity_kwh=60, consumption_kwh_per_km=.18))
            first = (await store.session(vehicle_id, uuid4()))["session_id"]
            def sample(session, seq, soc):
                return TelemetryUpdate(
                    session_id=session, sequence=seq, observed_at=utcnow(),
                    position={"latitude": 1.3501, "longitude": 103.851},
                    soc_pct=soc, speed_kph=11, odometer_km=3.2,
                    gps_quality="SIMULATED")
            original = sample(first, 0, 73)
            assert (await store.telemetry(vehicle_id, original))["accepted"]
            assert (await store.telemetry(vehicle_id, original))["duplicate"]
            assert len((await store.telemetry_history(vehicle_id))["samples"]) == 1
            next_sample = sample(first, 1, 72)
            await store.telemetry(vehicle_id, next_sample)
            async with store.repository.pool.acquire() as connection:
                count = await connection.fetchval(
                    "SELECT count(*) FROM telemetry_samples WHERE vehicle_id=$1",
                    vehicle_id)
            assert count == 2
        finally:
            await store.close()

        # New Fleet process must restore current state and historical samples.
        restarted = FleetStore(repository=SnapshotRepository("fleet", database))
        await restarted.start()
        try:
            current = await restarted.get(vehicle_id)
            assert current["telemetry"]["soc_pct"] == 72
            history = await restarted.telemetry_history(vehicle_id)
            assert history["storage"] == "postgresql"
            assert [row["sequence"] for row in history["samples"]] == [0, 1]
            assert history["samples"][0]["telemetry"]["gps_quality"] == "SIMULATED"
        finally:
            await restarted.close()
    asyncio.run(scenario())
