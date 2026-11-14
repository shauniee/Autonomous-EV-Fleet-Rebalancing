import asyncio
from contextlib import asynccontextmanager

import pytest


class FailingConnection:
    def __init__(self, failure):
        self.failure = failure
        self.started = asyncio.Event()

    @asynccontextmanager
    async def transaction(self):
        yield self
        if self.failure == "commit":
            raise ConnectionResetError("commit response lost")

    async def execute(self, *args):
        self.started.set()
        if self.failure in ("write", "commit"):
            raise ConnectionResetError(self.failure + " response lost")
        if self.failure == "cancel":
            await asyncio.Event().wait()

    async def executemany(self, *args):
        return None


class FailingPool:
    def __init__(self, failure):
        self.connection = FailingConnection(failure)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.parametrize("failure", ["write", "commit", "cancel"])
def test_record_transaction_uncertainty_permanently_fences_the_repository(failure):
    from ev_fleet.common.record_persistence import RecordSnapshotRepository

    async def scenario():
        repo = RecordSnapshotRepository("dispatch", "records", "postgresql://fixture")
        pool = FailingPool(failure)
        repo.pool = pool
        write = asyncio.create_task(repo.save({"version": 1, "records": {"one": {"n": 1}}},
                                              changed_keys={"one"}))
        if failure == "cancel":
            await pool.connection.started.wait()
            write.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ConnectionResetError):
            await write
        assert not repo.ready
        assert repo.faulted
        with pytest.raises(RuntimeError, match="PERSISTENCE_STATE_UNCERTAIN"):
            await repo.save({"version": 2, "records": {"one": {"n": 2}}}, changed_keys={"one"})
    asyncio.run(scenario())
