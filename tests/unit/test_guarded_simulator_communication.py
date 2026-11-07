import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx
from httpx import AsyncClient
import pytest

from ev_fleet.fleet.app import create_app
from ev_fleet.simulator.commands import MovingNode
from ev_fleet.simulator.recovery import (
    CheckpointRunner, RecoveryRequired, RetryableConflict,
)
from tests.unit.test_idle_simulator_recovery import (
    completed_parking, dispatch_response, idle_vehicle,
)


def http_error(status, code):
    request = httpx.Request("POST", "http://fleet/failure")
    response = httpx.Response(
        status, json={"error": {"code": code}}, request=request)
    return httpx.HTTPStatusError(code, request=request, response=response)


@asynccontextmanager
async def idle_runner(clock, monkeypatch, charging_state="NOT_CHARGING",
                      facilities=False):
    parking = (completed_parking() if charging_state != "NOT_CHARGING" else
               {"vehicle_id": "ev-idle", "pending": False, "parking": None})
    dispatch_response(monkeypatch, parking=parking)
    store, _, _ = await idle_vehicle(clock, charging_state)
    monkeypatch.setattr("ev_fleet.simulator.recovery.utcnow", clock.now)
    monkeypatch.setattr(
        "ev_fleet.simulator.recovery.time",
        SimpleNamespace(monotonic=clock.monotonic))
    async with AsyncClient(
            transport=httpx.ASGITransport(app=create_app(store)),
            base_url="http://fleet") as client:
        node = MovingNode(
            "http://fleet", "http://dispatch", "ev-idle",
            {"latitude": 1.4, "longitude": 103.9}, 10, client=client,
            checkpointing=True, facilities_url="http://facilities"
            if facilities or charging_state != "NOT_CHARGING" else None)
        node.traffic_enabled = False
        runner = CheckpointRunner(node)
        runner.blocked = "SIMULATOR_LEASE_EXPIRED_COORDINATION_REQUIRED"
        send = node.request
        requests = []

        async def dependencies(method, url, body=None, **kwargs):
            requests.append((method, url, deepcopy(body)))
            if url == "http://facilities/api/v1/vehicles/ev-idle/parking":
                return deepcopy(parking)
            if url == "http://dispatch/api/v1/vehicles/ev-idle/commands":
                return {"commands": []}
            return await send(method, url, body, **kwargs)

        node.request = dependencies
        await runner.try_handoff()
        await runner.tick()
        requests.clear()
        yield runner, store, requests, parking


@pytest.mark.parametrize("code", [
    "SIMULATOR_EVIDENCE_CHANGED", "SIMULATOR_CLAIM_MISMATCH",
])
def test_rejected_renewal_refreshes_evidence_without_replacing_owner(
        clock, monkeypatch, code):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, store, calls, _):
            owner = deepcopy(store.record("ev-idle")["simulator_owner"])
            send = runner.node.request
            rejected = False

            async def conflict(method, url, body=None, **kwargs):
                nonlocal rejected
                if method == "POST" and body.get("action") == "RENEW":
                    if not rejected:
                        rejected = True
                        calls.append((method, url, deepcopy(body)))
                        if code == "SIMULATOR_EVIDENCE_CHANGED":
                            store.record("ev-idle")["control_version"] += 1
                            return await send(method, url, body, **kwargs)
                        raise http_error(409, code)
                return await send(method, url, body, **kwargs)

            runner.node.request = conflict
            clock.advance(23)
            with pytest.raises(RetryableConflict, match=code):
                await runner.ensure_lease()
            assert runner.pending_session is None and runner.blocked is None
            assert store.record("ev-idle")["simulator_owner"] == owner
            await runner.tick()
            renewals = [body for method, _, body in calls if method == "POST"]
            assert renewals[0]["request_id"] != renewals[-1]["request_id"]
            assert runner.lease.generation == owner["generation"]
            assert runner.lease.owner_revision == owner["revision"] + 1
    asyncio.run(scenario())


def test_lost_renewal_response_is_replayed_even_after_old_expiry(
        clock, monkeypatch):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, store, calls, _):
            send = runner.node.request
            lost = False

            async def lose_response(method, url, body=None, **kwargs):
                nonlocal lost
                result = await send(method, url, body, **kwargs)
                if method == "POST" and body.get("action") == "RENEW" and not lost:
                    lost = True
                    raise httpx.ReadTimeout("renewal response lost after commit")
                return result

            runner.node.request = lose_response
            clock.advance(23)
            with pytest.raises(httpx.ReadTimeout):
                await runner.ensure_lease()
            pending = deepcopy(runner.pending_session)
            expiry = store.record("ev-idle")["simulator_owner"]["expires_at"]
            clock.advance(8)
            assert runner.lease.expires_at < clock.now()
            await runner.tick()
            renewals = [body for method, _, body in calls if method == "POST"]
            assert renewals == [pending, pending]
            assert runner.pending_session is None
            assert runner.lease.expires_at == expiry
            assert runner.lease.owner_revision == 2
            assert runner.node.movement.odometer_km == 15.2
    asyncio.run(scenario())


@pytest.mark.parametrize("outage", [23, 31])
def test_lost_checkpoint_is_reconciled_before_renewal_or_motion(
        clock, monkeypatch, outage):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, store, calls, _):
            send = runner.node.request
            lost = False

            async def lose_response(method, url, body=None, **kwargs):
                nonlocal lost
                result = await send(method, url, body, **kwargs)
                if method == "PUT" and not lost:
                    lost = True
                    raise httpx.ReadTimeout("checkpoint response lost after commit")
                return result

            runner.node.request = lose_response
            revision = runner.revision
            with pytest.raises(httpx.ReadTimeout):
                await runner.checkpoint()
            pending = deepcopy(runner.pending_checkpoint)
            assert runner.revision == revision
            clock.advance(outage)
            calls.clear()
            if outage == 31:
                with pytest.raises(RecoveryRequired, match="LEASE_EXPIRED"):
                    await runner.tick()
                assert all(method != "POST" for method, _, _ in calls)
            else:
                await runner.tick()
                renewal = next(body for method, _, body in calls
                               if method == "POST")
                assert renewal["expected_checkpoint_revision"] == revision + 1
            assert calls[0][0] == "PUT" and calls[0][2] == pending
            assert runner.pending_checkpoint is None
            assert runner.revision == revision + 1
            assert runner.node.movement.position == {"latitude": 1.32, "longitude": 103.81}
            assert store.record("ev-idle")["simulator_checkpoint"]["revision"] == revision + 1
    asyncio.run(scenario())


def test_renewal_precedes_polling_and_is_rechecked_between_dependencies(
        clock, monkeypatch):
    async def scenario():
        async with idle_runner(clock, monkeypatch, facilities=True) as data:
            runner, _, calls, _ = data
            send = runner.node.request

            async def slow_poll(method, url, body=None, **kwargs):
                if url.endswith("/commands"):
                    clock.advance(5)
                return await send(method, url, body, **kwargs)

            runner.node.request = slow_poll
            clock.advance(18)
            await runner.poll()
            command_index = next(i for i, (_, url, _) in enumerate(calls)
                                 if url.endswith("/commands"))
            renewal_index = next(i for i, (method, _, _) in enumerate(calls)
                                 if method == "POST")
            parking_index = next(i for i, (_, url, _) in enumerate(calls)
                                 if url.endswith("/parking"))
            assert command_index < renewal_index < parking_index
            assert runner.lease.owner_revision == 2
            calls.clear()
            clock.advance(23)
            await runner.poll()
            assert calls[0][1].endswith("/ev-idle")
            assert calls[1][0] == "POST"
            assert calls[2][1].endswith("/commands")
    asyncio.run(scenario())


def test_dependency_response_after_expiry_cannot_start_new_work(
        clock, monkeypatch):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, _, calls, _):
            send = runner.node.request

            async def expired_poll(method, url, body=None, **kwargs):
                if url.endswith("/commands"):
                    clock.advance(31)
                return await send(method, url, body, **kwargs)

            runner.node.request = expired_poll
            before = runner.node.movement.snapshot()
            with pytest.raises(RecoveryRequired, match="LEASE_EXPIRED"):
                await runner.poll()
            assert runner.node.movement.snapshot() == before
            assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["owner", "checkpoint"])
def test_renewal_blocks_changed_authoritative_owner_or_checkpoint(
        clock, monkeypatch, changed):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, store, calls, _):
            record = store.record("ev-idle")
            if changed == "owner":
                record["simulator_owner"]["revision"] += 1
            else:
                record["simulator_checkpoint"]["revision"] += 1
            clock.advance(23)
            with pytest.raises(RecoveryRequired, match="CHANGED"):
                await runner.ensure_lease()
            assert all(method != "POST" for method, _, _ in calls)
    asyncio.run(scenario())


def test_charging_baseline_rejection_reconciles_before_retry(
        clock, monkeypatch):
    async def scenario():
        async with idle_runner(clock, monkeypatch, "CHARGING") as data:
            runner, store, calls, _ = data
            before = runner.node.movement.soc_pct
            store.simulator_recovery.charging_epoch = str(uuid4())
            clock.advance(2)
            runner.advance()
            assert runner.node.movement.soc_pct > before
            await runner.checkpoint()
            writes = [body for method, _, body in calls if method == "PUT"]
            assert len(writes) == 2
            assert writes[0]["request_id"] != writes[1]["request_id"]
            assert runner.node.movement.soc_pct == before
            assert runner.pending_checkpoint is None
            assert (await store.get("ev-idle"))["telemetry"]["soc_pct"] == before
    asyncio.run(scenario())


def test_repeated_rejected_observations_retry_without_advancing_state(
        clock, monkeypatch):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, _, calls, _):
            send = runner.node.request
            rejected = []

            async def reject(method, url, body=None, **kwargs):
                if method == "PUT" and len(rejected) < 2:
                    rejected.append(deepcopy(body))
                    raise http_error(422, "OBSERVATION_TIME_INVALID")
                return await send(method, url, body, **kwargs)

            runner.node.request = reject
            before = runner.node.movement.snapshot()
            with pytest.raises(RetryableConflict, match="OBSERVATION_TIME_INVALID"):
                await runner.checkpoint()
            pending = deepcopy(runner.pending_checkpoint)
            assert pending["request_id"] not in [r["request_id"] for r in rejected]
            await runner.tick()
            assert calls[0][2] == pending
            assert runner.node.movement.snapshot() == before
            assert runner.pending_checkpoint is None
    asyncio.run(scenario())


class StopRunner(Exception):
    pass


@pytest.mark.parametrize("status", [408, 429, 500, 503, "transport", "race"])
def test_transient_run_failures_pause_with_bounded_backoff_and_resume(
        clock, monkeypatch, status):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, _, _, _):
            attempts = 0
            sleeps = []

            async def tick():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    if status == "transport":
                        raise httpx.ReadTimeout("temporary outage")
                    if status == "race":
                        raise RetryableConflict("SIMULATOR_EVIDENCE_CHANGED")
                    raise http_error(status, "TEMPORARILY_UNAVAILABLE")
                assert runner.blocked is None and not runner.node.ready.is_set()
                assert runner.last_motion == clock.monotonic()
                raise StopRunner()

            async def sleep(delay):
                sleeps.append(delay)
                clock.advance(delay)

            monkeypatch.setattr(runner, "tick", tick)
            monkeypatch.setattr("ev_fleet.simulator.recovery.asyncio",
                                SimpleNamespace(sleep=sleep))
            with pytest.raises(StopRunner):
                await runner.run()
            assert attempts == 2 and len(sleeps) == 1
            assert 0 < sleeps[0] <= runner.retry_max_delay
    asyncio.run(scenario())


@pytest.mark.parametrize("code", [
    "SIMULATOR_OWNER_CONFLICT", "SIMULATOR_CHECKPOINT_REVISION_CONFLICT",
    "SIMULATOR_REQUEST_ID_CONFLICT", "ACK_CONFLICT", "EVENT_SEQUENCE_CONFLICT",
])
def test_run_keeps_contradictory_execution_blocked(clock, monkeypatch, code):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, _, _, _):
            async def tick():
                raise http_error(409, code)

            async def handoff():
                assert runner.blocked == code
                assert not runner.node.ready.is_set()
                raise StopRunner()

            async def sleep(delay):
                clock.advance(delay)

            monkeypatch.setattr(runner, "tick", tick)
            monkeypatch.setattr(runner, "try_handoff", handoff)
            monkeypatch.setattr("ev_fleet.simulator.recovery.asyncio",
                                SimpleNamespace(sleep=sleep))
            with pytest.raises(StopRunner):
                await runner.run()
    asyncio.run(scenario())


def test_request_deadline_cancels_stalled_dependency():
    async def scenario():
        node = MovingNode("http://fleet", "http://dispatch", "ev-idle",
                          {"latitude": 1.32, "longitude": 103.81}, 85)
        runner = CheckpointRunner(node)
        runner.request_timeout = .005
        cancelled = False

        async def stalled(*args, **kwargs):
            nonlocal cancelled
            try:
                await asyncio.sleep(1)
            finally:
                cancelled = True

        node.request = stalled
        with pytest.raises(httpx.ReadTimeout, match="deadline"):
            await asyncio.wait_for(runner.request("GET", "http://fleet"), .2)
        assert cancelled
    asyncio.run(scenario())


def test_retry_jitter_is_capped_and_reserves_renewal_time(clock, monkeypatch):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, _, _, _):
            bounds = []

            def jitter(low, high):
                bounds.append((low, high))
                return high

            monkeypatch.setattr("ev_fleet.simulator.recovery.random.uniform", jitter)
            delays = [runner.retry_delay() for _ in range(10)]
            assert delays[:4] == [.25, .5, 1, 2]
            assert max(delays) == runner.retry_max_delay
            assert all(low == high / 2 for low, high in bounds)
            clock.advance(29.5)
            assert runner.retry_delay() == .01
            runner.blocked = "SIMULATOR_LEASE_EXPIRED_COORDINATION_REQUIRED"
            assert runner.retry_delay() == 5
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [408, 429, 503])
def test_restore_identity_survives_transient_error_after_commit(
        clock, monkeypatch, status):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, store, calls, _):
            send = runner.node.request
            lost = False

            async def lose_restore(method, url, body=None, **kwargs):
                nonlocal lost
                result = await send(method, url, body, **kwargs)
                if (method == "POST" and body.get("action") == "RESTORE_IDLE"
                        and not lost):
                    lost = True
                    raise http_error(status, "TEMPORARILY_UNAVAILABLE")
                return result

            runner.node.request = lose_restore
            clock.advance(31)
            runner.blocked = "SIMULATOR_LEASE_EXPIRED_COORDINATION_REQUIRED"
            with pytest.raises(httpx.HTTPStatusError):
                await runner.try_handoff()
            assert runner.pending_idle_restore is not None
            expiry = store.record("ev-idle")["simulator_owner"]["expires_at"]
            await runner.try_handoff()
            restores = [body for method, _, body in calls if method == "POST"]
            assert restores[0] == restores[1]
            assert runner.pending_idle_restore is None
            assert runner.lease.generation == 3
            assert runner.lease.expires_at == expiry
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["acknowledge", "flush_event"])
def test_late_dispatch_response_does_not_change_local_execution(
        clock, monkeypatch, operation):
    async def scenario():
        async with idle_runner(clock, monkeypatch) as (runner, _, _, _):
            command = {"dispatch_id": str(uuid4())}
            runner.node.movement.command = command
            runner.node.movement.accepted = True
            runner.node.movement.state = "DISPATCHED"
            runner.acknowledgement = {"accepted": True}
            event = {"sequence": 2, "kind": "PROGRESS"}
            runner.pending_event = deepcopy(event)

            async def late_response(method, url, body=None, **kwargs):
                assert method == "POST"
                assert kwargs["headers"] == {
                    "X-Simulator-Generation": str(runner.lease.generation),
                    "X-Simulator-Session": str(runner.lease.session_id)}
                clock.advance(31)
                return {}

            runner.node.request = late_response
            with pytest.raises(RecoveryRequired, match="LEASE_EXPIRED"):
                await getattr(runner, operation)()
            assert not runner.acknowledged
            assert runner.node.movement.state == "DISPATCHED"
            assert runner.pending_event == event
    asyncio.run(scenario())


def test_failed_checkpoint_backoff_does_not_credit_charging_time(
        clock, monkeypatch):
    async def scenario():
        async with idle_runner(clock, monkeypatch, "CHARGING") as data:
            runner, store, calls, _ = data
            send = runner.node.request
            lost = False
            sleeps = []

            async def failure(method, url, body=None, **kwargs):
                nonlocal lost
                if method == "PUT" and not lost:
                    lost = True
                    calls.append((method, url, deepcopy(body)))
                    raise http_error(503, "TEMPORARILY_UNAVAILABLE")
                return await send(method, url, body, **kwargs)

            async def sleep(delay):
                sleeps.append(delay)
                if len(sleeps) == 2:
                    raise StopRunner()
                clock.advance(delay)

            runner.node.request = failure
            runner.next_telemetry = 0
            runner.next_poll = clock.monotonic() + 100
            before = runner.node.movement.soc_pct
            monkeypatch.setattr(
                "ev_fleet.simulator.recovery.asyncio",
                SimpleNamespace(sleep=sleep, wait_for=asyncio.wait_for,
                                TimeoutError=asyncio.TimeoutError))
            with pytest.raises(StopRunner):
                await runner.run()
            writes = [body for method, _, body in calls if method == "PUT"]
            assert writes[0] == writes[1]
            assert runner.node.movement.soc_pct == before
            assert (await store.get("ev-idle"))["telemetry"]["soc_pct"] == before
            assert runner.failures == 0 and runner.node.ready.is_set()
    asyncio.run(scenario())


def test_initial_acquire_rebuilds_only_after_definitive_rejection(
        clock, monkeypatch):
    from ev_fleet.common.settings import SimulatorRecoverySettings
    from ev_fleet.fleet.store import FleetStore
    from tests.unit.test_idle_simulator_recovery import CheckpointRepository

    async def scenario():
        store = FleetStore(
            clock=clock, repository=CheckpointRepository(),
            recovery_settings=SimulatorRecoverySettings(
                enabled=True, vehicle_ids=["ev-idle"]))
        await store.start()
        monkeypatch.setattr("ev_fleet.simulator.recovery.utcnow", clock.now)
        async with AsyncClient(
                transport=httpx.ASGITransport(app=create_app(store)),
                base_url="http://fleet") as client:
            node = MovingNode(
                "http://fleet", "http://dispatch", "ev-idle",
                {"latitude": 1.32, "longitude": 103.81}, 85,
                client=client, checkpointing=True, road_match_initial=False)
            node.traffic_enabled = False
            runner = CheckpointRunner(node)
            send = node.request
            acquires = []

            async def race(method, url, body=None, **kwargs):
                if url.endswith("/commands"):
                    return {"commands": []}
                if method == "POST" and body.get("action") == "ACQUIRE":
                    acquires.append(deepcopy(body))
                    if len(acquires) == 1:
                        store.record("ev-idle")["control_version"] += 1
                return await send(method, url, body, **kwargs)

            node.request = race
            with pytest.raises(RetryableConflict):
                await runner.tick()
            assert runner.pending_session is None and runner.lease is None
            await runner.tick()
            assert acquires[0]["request_id"] != acquires[1]["request_id"]
            assert runner.lease.generation == 1
            assert node.ready.is_set()
    asyncio.run(scenario())


def test_parking_deadline_invalidates_expired_charging_authority(
        clock, monkeypatch):
    async def scenario():
        async with idle_runner(clock, monkeypatch, "CHARGING") as data:
            runner, _, _, _ = data
            runner.node.parking_verified_at -= (
                runner.node.facilities_grace_seconds + 1)
            runner.request_timeout = .005

            async def stalled():
                await asyncio.sleep(1)

            runner.node.refresh_parking = stalled
            with pytest.raises(httpx.ReadTimeout, match="Parking request"):
                await asyncio.wait_for(runner.parking(), .2)
            assert not runner.node.parking_verified
            assert runner.node.movement.charging_state == "NOT_CHARGING"
    asyncio.run(scenario())
