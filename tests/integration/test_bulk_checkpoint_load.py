"""Isolated write-burst comparator; paced end-to-end tests live alongside it."""
import asyncio
from copy import deepcopy
import json
import time
from uuid import uuid4


from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.common.record_persistence import RecordSnapshotRepository
from ev_fleet.contracts.common import utcnow


def timings(values):
    values = sorted(values)
    return {"count": len(values), **{label: round(values[min(len(values)-1, int(len(values)*fraction))], 2)
            for label, fraction in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99), ("max_ms", 1))}} if values else {"count": 0}


def test_fifty_overlapping_checkpoint_writes_stay_below_request_deadline(telemetry_database_url):
    async def scenario():
        bootstrap = SnapshotRepository("fleet", telemetry_database_url)
        await bootstrap.start()
        await bootstrap.close()
        geometry = [{"latitude": 1.32 + i*.0001, "longitude": 103.8 + i*.0001} for i in range(300)]
        fixture = {"version": 0, "vehicles": {str(i): {"distance": 0, "route": geometry,
                   "events": [{"sequence": n, "distance": n/100} for n in range(20)]} for i in range(50)}}
        results = {}
        for mode in ("legacy", "partitioned"):
            namespace = "bulk-" + uuid4().hex
            repo = (SnapshotRepository(namespace, telemetry_database_url) if mode == "legacy"
                    else RecordSnapshotRepository(namespace, "vehicles", telemetry_database_url))
            await repo.start()
            payload = deepcopy(fixture)
            await repo.save(payload)
            lock = asyncio.Lock()
            durations = []

            async def update(index):
                started = time.perf_counter()
                key = str(index)
                async with lock:
                    payload["version"] += 1
                    payload["vehicles"][key]["distance"] = 1
                    sample = {"vehicle_id": namespace + "-" + key, "session_id": uuid4(), "sequence": 1,
                              "observed_at": utcnow(), "received_at": utcnow(), "telemetry": {"soc_pct": 80}}
                    if mode == "legacy":
                        await repo.save(payload, telemetry_sample=sample)
                    else:
                        await repo.save(payload, changed_keys={key}, telemetry_sample=sample)
                durations.append((time.perf_counter()-started)*1000)

            try:
                await asyncio.gather(*(update(index) for index in range(50)))
                assert await repo.load() == payload
                results[mode] = timings(durations)
            finally:
                await repo.close()
        print("BULK_STORAGE_TIMINGS=" + json.dumps(results), flush=True)
        assert results["partitioned"]["max_ms"] < 2000, results
    asyncio.run(scenario())
