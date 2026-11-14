"""One durable human approval, fifty feasible moves, no manual retry calls."""
import asyncio
import json
import time
from uuid import uuid4

import httpx

from ev_fleet.common.record_persistence import RecordSnapshotRepository
from tests.bulk_http_support import bulk_http_cluster, distributed_ev_drivers
from tests.integration.test_bulk_checkpoint_load import timings


def test_fifty_queued_relocations_complete_with_automatic_pacing(telemetry_database_url, tmp_path):
    async def scenario():
        async with bulk_http_cluster(telemetry_database_url, tmp_path, paced=True) as (urls, database_url):
            async with distributed_ev_drivers(urls, tmp_path, paced=True) as observe:
                async with httpx.AsyncClient(timeout=30, trust_env=False) as operator:
                    deadline = time.monotonic() + 60
                    while True:
                        response = await operator.get(urls["fleet"] + "/api/v1/dashboard/vehicles")
                        response.raise_for_status()
                        fleet = response.json()["vehicles"]
                        if len(fleet) == 50 and all(v["effective_availability"] == "AVAILABLE" for v in fleet):
                            break
                        assert time.monotonic() < deadline, observe()
                        await asyncio.sleep(.1)
                    proposal = await operator.post(urls["rebalancing"] + "/api/v1/batch-recommendations",
                        json={"target_zone_id": "cbd", "quantity": 50, "planning_mode": "FULFIL_REQUEST"})
                    proposal.raise_for_status()
                    plan = proposal.json()
                    assert plan["recommended_count"] == 50, plan
                    request_id = str(uuid4())
                    started = time.monotonic()
                    accepted = await operator.post(urls["dispatch"] + "/api/v1/batches", json={
                        "request_id": request_id, "batch_recommendation_id": plan["batch_recommendation_id"]})
                    accepted.raise_for_status()
                    acceptance_seconds = time.monotonic() - started
                    assert acceptance_seconds < 5, acceptance_seconds
                    assert accepted.json()["job_status"] in ("QUEUED", "RUNNING")
                    max_active = 0
                    deadline = time.monotonic() + 300
                    while True:
                        reply = await operator.get(urls["dispatch"] + "/api/v1/batches/" + request_id)
                        reply.raise_for_status()
                        job = reply.json()
                        response = await operator.get(urls["fleet"] + "/api/v1/dashboard/vehicles")
                        response.raise_for_status()
                        active = sum(v["active_claim"] is not None for v in response.json()["vehicles"])
                        max_active = max(max_active, active)
                        assert active <= 4, {"active": active, "job": job}
                        assert not any(reason for state in observe() for reason in state["blocked"]), observe()
                        if job["job_status"] in ("COMPLETED", "EXPIRED"):
                            break
                        assert time.monotonic() < deadline, {"job": job, "workers": observe()}
                        await asyncio.sleep(.5)
                    states = observe()
                    retry_modes = [item for state in states for item in state["retries"]]
                    values = {kind: [value for state in states for value in state["measurements"][kind]]
                              for kind in ("checkpoint", "events", "other")}
                    summary = {"job_status": job["job_status"], "completed": job["completed_count"],
                        "admitted": job["admitted_count"], "max_active": max_active,
                        "acceptance_seconds": round(acceptance_seconds, 3),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "retry_modes": retry_modes, "simulation_time_scale": 30,
                        "timings": {kind: timings(items) for kind, items in values.items()}}
                    print("PACED_BULK_RESULTS=" + json.dumps(summary), flush=True)
                    assert job["job_status"] == "COMPLETED" and job["completed_count"] == 50, summary
                    assert job["admitted_count"] == 50 and job["queued_count"] == 0, job
                    assert not any(item["mode"] == "RECOVERY_WAIT" for item in retry_modes), summary
                    reader = RecordSnapshotRepository("dispatch", "records", database_url)
                    await reader.start()
                    try:
                        records = (await reader.load())["records"]
                        assert len(records) == 50
                        assert len({record["data"]["vehicle_id"] for record in records.values()}) == 50
                        assert all(record["data"]["state"] == "SUCCEEDED" for record in records.values())
                    finally:
                        await reader.close()
    asyncio.run(scenario())
