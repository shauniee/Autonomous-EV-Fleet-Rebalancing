import asyncio
from copy import deepcopy
from datetime import datetime
from fastapi import HTTPException
from ev_fleet.common.persistence import SnapshotRepository


class RecommendationStore:
    def __init__(self, clock, namespace="rebalancing.recommendations", repository=None):
        self.clock = clock
        self.lock = asyncio.Lock()
        self.records = {}
        self.repository = repository or SnapshotRepository.from_env(namespace)

    async def start(self):
        await self.repository.start()
        payload = await self.repository.load()
        if not payload:
            await self.persist()
            return
        now, tick = self.clock.now(), self.clock.monotonic()
        for key, record in payload.items():
            expires_at = datetime.fromisoformat(record["expires_at"])
            remaining = (expires_at - now).total_seconds()
            if remaining > 0:
                self.records[key] = (tick + remaining, record)

    async def close(self):
        await self.repository.close()

    async def persist(self):
        await self.repository.save({key: value[1] for key, value in self.records.items()})

    def prune(self):
        now = self.clock.monotonic()
        self.records = {key: value for key, value in self.records.items() if value[0] > now}

    async def put(self, record, ttl):
        await self.put_many([record], ttl=ttl)

    async def put_many(self, records, *, ttl=None):
        async with self.lock:
            self.prune()
            now, tick = self.clock.now(), self.clock.monotonic()
            pending = {}
            for record in records:
                remaining = ttl if ttl is not None else (datetime.fromisoformat(record["expires_at"]) - now).total_seconds()
                if remaining > 0:
                    pending[record["recommendation_id"]] = (tick + remaining, deepcopy(record))
            if len(self.records.keys() | pending.keys()) > 10000:
                raise HTTPException(503, "RECOMMENDATION_CAPACITY_REACHED")
            if pending:
                await self.repository.save({key: value[1] for key, value in (self.records | pending).items()})
                self.records.update(pending)

    async def get(self, key):
        async with self.lock:
            self.prune()
            if key not in self.records:
                raise HTTPException(404, "RECOMMENDATION_MISSING_OR_EXPIRED")
            return deepcopy(self.records[key][1])
