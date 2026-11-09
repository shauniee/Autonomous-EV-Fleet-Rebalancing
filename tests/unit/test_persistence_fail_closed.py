"""A lost commit response must not make in-memory fleet state authoritative."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from ev_fleet.common.logging import base_app
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.rebalancing.app import create_app as rebalancing_app


class ConnectionWithLostCommit:
    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, *_args):
        raise ConnectionResetError("database transport lost")


class PoolWithLostCommit:
    @asynccontextmanager
    async def acquire(self):
        yield ConnectionWithLostCommit()


def test_ambiguous_snapshot_write_blocks_new_requests_until_restart():
    async def scenario():
        repo = SnapshotRepository("fleet", url="postgresql://fixture")
        repo.pool = PoolWithLostCommit()
        assert repo.ready
        with pytest.raises(ConnectionResetError):
            await repo.save({"vehicles": {"ev-a": 1}})
        assert repo.faulted is True
        assert repo.ready is False
        assert repo.status() == "uncertain"
        with pytest.raises(RuntimeError, match="PERSISTENCE_STATE_UNCERTAIN"):
            await repo.save({"vehicles": {"ev-a": 2}})

        app = base_app("Persistence Guard Test")
        app.state.persistence = repo

        @app.get("/api/v1/vehicles")
        async def vehicles():
            return {"vehicles": [{"vehicle_id": "ev-a"}]}

        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fleet.test") as client:
            ready = await client.get("/health/ready")
            assert ready.status_code == 503
            live = await client.get("/health/live")
            assert live.status_code == 200
            blocked = await client.get("/api/v1/vehicles")
            assert blocked.status_code == 503
            assert blocked.json()["error"]["code"] == "PERSISTENCE_STATE_UNCERTAIN"
            assert blocked.headers["X-Request-ID"]
    asyncio.run(scenario())


def test_ephemeral_repository_stays_available_without_postgres():
    async def scenario():
        repo = SnapshotRepository("fleet", url=None)
        await repo.save({"vehicles": {}})
        assert repo.ready is True
        assert repo.faulted is False
    asyncio.run(scenario())


def test_rebalancing_schedules_recovery_after_an_ambiguous_snapshot_write(monkeypatch):
    exits = []

    class RecordingTimer:
        def __init__(self, delay, callback, args):
            self.args = args

        def start(self):
            exits.append(self.args)

    monkeypatch.setattr("ev_fleet.common.persistence.Timer", RecordingTimer)
    app = rebalancing_app()

    async def scenario():
        async with app.router.lifespan_context(app):
            repo = app.state.persistence[0]
            repo.url = "postgresql://fixture"
            repo.pool = PoolWithLostCommit()
            try:
                with pytest.raises(ConnectionResetError):
                    await repo.save({"recommendation": "fixture"})
            finally:
                repo.pool = None
            assert exits == [(75,)]

    asyncio.run(scenario())
