"""Bounded durable checkpoint and renewal groups; each receipt waits for confirmed commit."""
import asyncio
import logging
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException

from ev_fleet.contracts.recovery import (
    SimulatorCheckpointRequest, SimulatorCheckpointReceipt,
    SimulatorSessionRequest, SimulatorSession,
)


@dataclass
class PendingCheckpoint:
    vehicle_id: str
    body: SimulatorCheckpointRequest | SimulatorSessionRequest
    future: asyncio.Future[SimulatorCheckpointReceipt | SimulatorSession]


class CheckpointWriter:
    max_pending = 1024
    max_batch = 64
    collect_seconds = .002

    def __init__(self, recovery):
        self.recovery = recovery
        self.pending = deque()
        self.wake = asyncio.Event()
        self.task = None
        self.closed = False

    async def submit(self, vehicle_id, body):
        if self.closed or not self.recovery.store.ready:
            raise HTTPException(503, "SIMULATOR_PERSISTENCE_UNAVAILABLE")
        if len(self.pending) >= self.max_pending:
            raise HTTPException(503, "SIMULATOR_CHECKPOINT_BACKLOG")
        future = asyncio.get_running_loop().create_future()
        # A disconnected caller does not cancel a shared transaction. Consume
        # orphaned exceptions; a caller still awaiting the future sees them too.
        future.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)
        self.pending.append(PendingCheckpoint(vehicle_id, body, future))
        self.wake.set()
        if self.task is None:
            self.task = asyncio.create_task(self.run())
        return await asyncio.shield(future)

    async def close(self):
        self.closed = True
        self.wake.set()
        if self.task is not None:
            await self.task

    async def run(self):
        batch = []
        try:
            while True:
                if not self.pending:
                    if self.closed:
                        return
                    self.wake.clear()
                    await self.wake.wait()
                    continue
                await asyncio.sleep(self.collect_seconds)
                batch, deferred, vehicles = [], deque(), set()
                while self.pending:
                    item = self.pending.popleft()
                    if len(batch) < self.max_batch and item.vehicle_id not in vehicles:
                        batch.append(item)
                        vehicles.add(item.vehicle_id)
                    else:
                        deferred.append(item)
                self.pending = deferred
                await self.recovery.commit_checkpoint_group(batch)
                if not self.recovery.store.ready:
                    raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
        except BaseException as exc:
            self.closed = True
            self.recovery.store.mark_persistence_uncertain()
            logging.getLogger(__name__).exception("Checkpoint group failed; Fleet authority is fenced")
            for item in [*batch, *self.pending]:
                if not item.future.done():
                    item.future.set_exception(HTTPException(503, "SIMULATOR_PERSISTENCE_UNCERTAIN"))
            self.pending.clear()
            if not isinstance(exc, Exception):
                raise
