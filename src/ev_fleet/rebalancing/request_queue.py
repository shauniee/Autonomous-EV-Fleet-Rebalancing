"""Single-writer CRDS request queue backed by the service snapshot repository."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
from typing import Any, TypedDict

from fastapi import HTTPException

from ev_fleet.common.clock import Clock
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.recommendations import (
    BatchRecommendation,
    QueuedRecommendationRequest,
    RecommendationRequestRecord,
    RecommendationRequestStatus,
)

ACTIVE = ("QUEUED", "EVALUATING")
HISTORY_AGE = timedelta(days=7)
HISTORY_LIMIT = 500


class Checkpoint(TypedDict):
    batch: dict[str, Any]
    children: list[dict[str, Any]]


class Entry(TypedDict):
    record: RecommendationRequestRecord
    checkpoint: Checkpoint | None


class RecommendationQueue:
    def __init__(self, clock: Clock, repository: SnapshotRepository, *,
                 capacity: int, max_wait_seconds: float):
        self.clock = clock
        self.repository = repository
        self.capacity = capacity
        self.max_wait = timedelta(seconds=max_wait_seconds)
        self.entries: dict[str, Entry] = {}
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.stopping = False

    async def start(self):
        self.entries = await self.repository.load() or {}
        self.stopping = False

    def stop(self):
        self.stopping = True
        self.wake.set()

    def retained(self, entries: dict[str, Entry]) -> dict[str, Entry]:
        cutoff = self.clock.now() - HISTORY_AGE
        completed = sorted((finished, key) for key, entry in entries.items()
                           if (finished := entry["record"].finished_at) and finished > cutoff)
        keep = {key for _, key in completed[-HISTORY_LIMIT:]}
        return {key: entry for key, entry in entries.items()
                if key in keep or entry["record"].status in ACTIVE}

    async def save(self, key: str, record: RecommendationRequestRecord,
                   checkpoint: Checkpoint | None = None):
        # Call under the lock. Expose the new state only after the write succeeds.
        entry: Entry = {"record": record, "checkpoint": checkpoint}
        entries = self.retained(self.entries | {key: entry})
        await self.repository.save(entries)
        self.entries = entries

    def view(self, record: RecommendationRequestRecord) -> RecommendationRequestRecord:
        if record.status == "READY" and record.expires_at and record.expires_at <= self.clock.now():
            return record.model_copy(update={"status": "EXPIRED"})
        return record

    async def submit(self, request: QueuedRecommendationRequest) -> RecommendationRequestRecord:
        async with self.lock:
            key = str(request.request_id)
            existing = self.retained(self.entries).get(key)
            if existing:
                record = existing["record"]
                if record.model_dump(include=set(QueuedRecommendationRequest.model_fields)) != request.model_dump():
                    raise HTTPException(409, "RECOMMENDATION_REQUEST_ID_CONFLICT")
                return self.view(record)
            if sum(entry["record"].status in ACTIVE for entry in self.entries.values()) >= self.capacity:
                raise HTTPException(429, "RECOMMENDATION_QUEUE_FULL")
            record = RecommendationRequestRecord(**request.model_dump(), status="QUEUED",
                                                 submitted_at=self.clock.now())
            await self.save(key, record)
            self.wake.set()
            return record

    async def get(self, key: str) -> RecommendationRequestRecord:
        async with self.lock:
            entry = self.retained(self.entries).get(key)
            if entry is None:
                raise HTTPException(404, "RECOMMENDATION_REQUEST_NOT_FOUND")
            return self.view(entry["record"])

    async def page(self, status: RecommendationRequestStatus | None, offset: int, limit: int):
        async with self.lock:
            records = [self.view(entry["record"]) for entry in reversed(self.retained(self.entries).values())]
            filtered = [record for record in records if status is None or record.status == status]
            end = offset + limit
            return filtered[offset:end], end if end < len(filtered) else None

    async def finish(self, record: RecommendationRequestRecord, **updates: Any):
        async with self.lock:
            await self.save(str(record.request_id), RecommendationRequestRecord.model_validate({
                **record.model_dump(), **updates, "finished_at": self.clock.now(),
            }))

    async def run(self, evaluate: Callable[[QueuedRecommendationRequest], Awaitable[Checkpoint]],
                  publish: Callable[[Checkpoint], Awaitable[None]]):
        while not self.stopping:
            async with self.lock:
                entry = next((entry for entry in self.entries.values()
                              if entry["record"].status in ACTIVE), None)
                if entry is None:
                    self.wake.clear()
            if entry is None:
                await self.wake.wait()
            else:
                await self.process(entry, evaluate, publish)

    async def process(self, entry: Entry,
                      evaluate: Callable[[QueuedRecommendationRequest], Awaitable[Checkpoint]],
                      publish: Callable[[Checkpoint], Awaitable[None]]):
        record, checkpoint = entry["record"], entry["checkpoint"]
        key = str(record.request_id)
        if checkpoint is None:
            now = self.clock.now()
            if now - record.submitted_at >= self.max_wait:
                await self.finish(record, status="DEFERRED", reason_code="QUEUE_WAIT_EXCEEDED")
                return
            record = record.model_copy(update={"status": "EVALUATING", "started_at": now})
            async with self.lock:
                await self.save(key, record)
            try:
                checkpoint = await evaluate(record)
            except HTTPException as exc:
                reason = exc.detail if isinstance(exc.detail, str) else "PLANNING_FAILED"
                await self.finish(record, status="FAILED", reason_code=reason)
                return
            except Exception:
                logging.getLogger(__name__).exception("Queued CRDS planning failed for %s", key)
                await self.finish(record, status="FAILED", reason_code="PLANNING_FAILED")
                return
            async with self.lock:
                await self.save(key, record, checkpoint)

        batch = BatchRecommendation.model_validate(checkpoint["batch"])
        assert batch.crds is not None
        status: RecommendationRequestStatus
        if batch.crds.decision == "DEFER":
            status = "DEFERRED"
        elif batch.crds.decision == "HOLD":
            status = "HOLD"
        elif not batch.crds.approval_allowed:
            status = "ADVISORY"
        else:
            status = "READY"
        reason = batch.crds.reason_code
        if batch.expires_at > self.clock.now():
            try:
                await publish(checkpoint)
            except HTTPException as exc:
                status = "FAILED"
                reason = str(exc.detail)
        if status == "READY" and batch.expires_at <= self.clock.now():
            status = "EXPIRED"
        await self.finish(record, status=status, reason_code=reason, decision=batch.crds.decision,
                          batch_recommendation_id=batch.batch_recommendation_id,
                          recommended_count=batch.recommended_count, expires_at=batch.expires_at,
                          assignments=[item.model_dump(include={"vehicle_id", "facility_id", "facility_name"})
                                       for item in batch.assignments])
