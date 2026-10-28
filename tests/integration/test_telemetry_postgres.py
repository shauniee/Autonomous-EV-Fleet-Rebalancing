"""Real PostgreSQL integration: latest state plus append-only unique history.

Run only in a disposable test database with TELEMETRY_TEST_DATABASE_URL.
Do NOT point this test at a live Kubernetes/PostgreSQL database.
"""
import asyncio
import os
from uuid import uuid4

import pytest

from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.common import utcnow
from ev_fleet.contracts.vehicles import TelemetryUpdate, VehicleRegistration
from ev_fleet.fleet.store import FleetStore


@pytest.mark.skipif(not os.getenv("TELEMETRY_TEST_DATABASE_URL"),
                    reason="Disposable Postgres test database not configured")
def test_history_is_durable_atomic_and_unique_between_sessions():
    async def scenario():
        database = os.environ["TELEMETRY_TEST_DATABASE_URL"]
        store = FleetStore(repository=SnapshotRepository("fleet", database))
        await store.start()
        try:
            await store.register(VehicleRegistration(vehicle_id="ev-postgres-test",
                battery_capacity_kwh=60, consumption_kwh_per_km=.18))
            first = (await store.session("ev-postgres-test", uuid4()))["session_id"]
            def sample(session, seq, soc):
                return TelemetryUpdate(
                    session_id=session, sequence=seq, observed_at=utcnow(),
                    position={"latitude": 1.3501, "longitude": 103.851},
                    soc_pct=soc, speed_kph=11, odometer_km=3.2,
                    gps_quality="SIMULATED")
            original = sample(first, 0, 73)
            assert (await store.telemetry("ev-postgres-test", original))["accepted"]
            assert (await store.telemetry("ev-postgres-test", original))["duplicate"]
            assert len((await store.telemetry_history("ev-postgres-test"))["samples"]) == 1
            next_sample = sample(first, 1, 72)
            await store.telemetry("ev-postgres-test", next_sample)
            async with store.repository.pool.acquire() as connection:
                count = await connection.fetchval(
                    "SELECT count(*) FROM telemetry_samples WHERE vehicle_id=$1",
                    "ev-postgres-test")
            assert count == 2
        finally:
            await store.close()

        # New Fleet process must restore current state and historical samples.
        restarted = FleetStore(repository=SnapshotRepository("fleet", database))
        await restarted.start()
        try:
            current = await restarted.get("ev-postgres-test")
            assert current["telemetry"]["soc_pct"] == 72
            history = await restarted.telemetry_history("ev-postgres-test")
            assert history["storage"] == "postgresql"
            assert [row["sequence"] for row in history["samples"]] == [0, 1]
            assert history["samples"][0]["telemetry"]["gps_quality"] == "SIMULATED"
        finally:
            await restarted.close()
    asyncio.run(scenario())
