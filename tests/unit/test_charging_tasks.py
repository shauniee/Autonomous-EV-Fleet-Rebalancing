import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ev_fleet.contracts.charging_tasks import ChargingTaskCreate, ChargingTaskUpdate
from tests.phase2_support import Cluster


def test_charging_task_is_explicit_durable_and_blocks_dispatch(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle(soc=25)
            initial = await cluster.fleet.get("ev-1")
            create = ChargingTaskCreate(vehicle_id="ev-1", request_id=uuid4(),
                                        expected_version=initial["state_version"])
            task = await cluster.fleet.create_charging_task(create)
            assert task["status"] == "PENDING"
            assert task == await cluster.fleet.create_charging_task(create)
            vehicle = await cluster.fleet.get("ev-1")
            assert vehicle["effective_availability"] == "UNAVAILABLE"
            assert vehicle["active_charging_task"]["task_id"] == task["task_id"]
            result = await cluster.recommend()
            assert result["selected_vehicle_id"] is None
            assert any("CHARGING_TASK" in c["exclusion_reasons"]
                       for c in result["excluded_candidates"])

            assigned = ChargingTaskUpdate(request_id=uuid4(),
                        expected_version=vehicle["state_version"], action="ASSIGN",
                        staff_name="Ground Team A")
            updated = await cluster.fleet.update_charging_task(task["task_id"], assigned)
            assert updated["status"] == "ASSIGNED"
            assert updated == await cluster.fleet.update_charging_task(task["task_id"], assigned)
            vehicle = await cluster.fleet.get("ev-1")
            start = ChargingTaskUpdate(request_id=uuid4(),
                        expected_version=vehicle["state_version"], action="START_WORK")
            assert (await cluster.fleet.update_charging_task(task["task_id"], start))["status"] == "IN_PROGRESS"
            vehicle = await cluster.fleet.get("ev-1")
            complete = ChargingTaskUpdate(request_id=uuid4(),
                        expected_version=vehicle["state_version"], action="COMPLETE")
            with pytest.raises(HTTPException) as error:
                await cluster.fleet.update_charging_task(task["task_id"], complete)
            assert error.value.detail == "CHARGING_EVIDENCE_REQUIRED"
            assert (await cluster.fleet.list_charging_tasks())["tasks"][0]["status"] == "IN_PROGRESS"
    asyncio.run(scenario())


def test_charging_tasks_refuse_busy_or_high_soc_vehicles(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle(soc=80)
            vehicle = await cluster.fleet.get("ev-1")
            body = ChargingTaskCreate(vehicle_id="ev-1", request_id=uuid4(),
                                      expected_version=vehicle["state_version"])
            with pytest.raises(HTTPException) as error:
                await cluster.fleet.create_charging_task(body)
            assert error.value.detail == "NOT_ELIGIBLE_FOR_CHARGING_TASK"
    asyncio.run(scenario())
