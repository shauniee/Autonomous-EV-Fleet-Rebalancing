import asyncio
from contextlib import suppress
import time
import pytest
from ev_fleet.common.clock import Clock
from ev_fleet.simulator.commands import MovingNode
from tests.phase2_support import Cluster


def test_movement_continues_through_network_outage_then_reports_arrival():
    async def scenario():
        async with Cluster(Clock()) as cluster:
            cluster.config.stale_seconds = .2
            cluster.config.offline_seconds = .3
            node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-1",
                {"latitude": 1.35, "longitude": 103.8513}, 80,
                telemetry_interval=.03, poll_interval=.03, tick_seconds=.01,
                speed_kph=30, time_scale=360, client=cluster.client)
            # Establish the claim before starting frequent telemetry; claim-race behaviour
            # is covered separately and must not make this outage test timing-dependent.
            await node.send_telemetry()
            dispatch, _ = await cluster.approve()
            task = asyncio.create_task(node.run())
            try:
                await asyncio.wait_for(node.ready.wait(), 2)
                deadline = time.monotonic() + 3
                while node.movement.state != "RELOCATING":
                    assert not task.done(), task.exception() if task.done() else None
                    assert time.monotonic() < deadline
                    await asyncio.sleep(.01)
                cluster.failures.extend([
                    {"method": "POST", "path": "fleet.test/api/v1/vehicles/ev-1/telemetry", "action": "offline", "once": False},
                    {"method": "GET", "path": "dispatch.test/", "action": "offline", "once": False},
                    {"method": "POST", "path": "dispatch.test/", "action": "offline", "once": False}])
                distance_before = node.movement.distance
                await asyncio.sleep(.4)
                assert node.movement.distance > distance_before
                result = await cluster.dispatch.get(dispatch["dispatch_id"])
                assert result["state"] == "UNKNOWN" and result["claim_held"]
                cluster.failures.clear()
                deadline = time.monotonic() + 8
                while node.movement.command is not None:
                    assert not task.done(), task.exception() if task.done() else None
                    assert time.monotonic() < deadline, await cluster.dispatch.get(dispatch["dispatch_id"])
                    await asyncio.sleep(.03)
                assert (await cluster.dispatch.get(dispatch["dispatch_id"]))["state"] == "SUCCEEDED"
                assert (await cluster.fleet.get("ev-1"))["current_zone_id"] == "cbd"
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    asyncio.run(scenario())


def test_new_node_cannot_overwrite_unresolved_journey(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            await cluster.approve()
            node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-1",
                {"latitude": 1.35, "longitude": 103.8513}, 80, client=cluster.client)
            with pytest.raises(RuntimeError, match="unresolved journey"):
                await node.register()
    asyncio.run(scenario())
