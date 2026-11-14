"""Optional phase timings for the disposable bulk harness only."""
import asyncio
import json
from time import perf_counter

import asyncpg


def install():
    from ev_fleet.common.record_persistence import RecordSnapshotRepository
    from ev_fleet.fleet.recovery import SimulatorRecovery
    from ev_fleet.fleet.store import FleetStore

    def report(phase, started):
        print("BULK_PHASE=" + json.dumps({"phase": phase,
            "ms": (perf_counter() - started) * 1000}), flush=True)

    def timed_async(owner, name, phase, predicate=None):
        original = getattr(owner, name)
        async def measured(*args, **kwargs):
            started = perf_counter()
            try:
                return await original(*args, **kwargs)
            finally:
                if predicate is None or predicate(*args, **kwargs):
                    report(phase, started)
        setattr(owner, name, measured)

    def timed_sync(owner, name, phase):
        original = getattr(owner, name)
        def measured(*args, **kwargs):
            started = perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                report(phase, started)
        setattr(owner, name, measured)

    timed_async(RecordSnapshotRepository, "save", "save")
    timed_async(SimulatorRecovery, "commit_checkpoint_group", "group")
    timed_async(asyncpg.Connection, "execute", "sql",
        lambda connection, query, *args, **kwargs: "written_header" in query)
    timed_async(asyncpg.Pool, "release", "release")
    timed_async(asyncio, "to_thread", "encode",
        lambda func, *args, **kwargs: "RecordSnapshotRepository.save" in func.__qualname__)
    timed_sync(SimulatorRecovery, "stage_checkpoint", "stage")
    timed_sync(FleetStore, "_publish_committed_updates", "publish")
