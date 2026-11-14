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
        bootstrap = SnapshotRepository("fleet", database)
        await bootstrap.start()
        await bootstrap.close()
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


def test_dispatch_idle_polls_preserve_postgres_journal_without_rewriting_it(telemetry_database_url):
    from ev_fleet.contracts.dispatch import DispatchApproval
    from ev_fleet.dispatch.service import DispatchService

    async def scenario():
        namespace = "dispatch-idle-test-" + uuid4().hex
        repository = SnapshotRepository(namespace, telemetry_database_url)
        service = DispatchService(None, repository=repository)
        await service.start()
        try:
            for index in range(200):
                record = await service.store.begin(DispatchApproval(
                    request_id=uuid4(), recommendation_id=uuid4()))
                record.data.update(
                    vehicle_id=f"history-{index}", state="SUCCEEDED",
                    revalidated_metrics={"history_fixture": "x" * 2500},
                )
            await service.persist()
            async with repository.pool.acquire() as connection:
                revision = await connection.fetchval(
                    "SELECT revision FROM service_snapshots WHERE namespace=$1", namespace)
            results = await asyncio.gather(*(
                service.commands(f"idle-{index}") for index in range(100)))
            assert all(result == {"commands": []} for result in results)
            async with repository.pool.acquire() as connection:
                after = await connection.fetchval(
                    "SELECT revision FROM service_snapshots WHERE namespace=$1", namespace)
            assert after == revision
        finally:
            await service.close()
        restored = DispatchService(None, repository=SnapshotRepository(
            namespace, telemetry_database_url))
        await restored.start()
        try:
            assert len(restored.store.records) == 200
            assert all(r.data["state"] == "SUCCEEDED"
                       for r in restored.store.records.values())
        finally:
            await restored.close()
    asyncio.run(scenario())
