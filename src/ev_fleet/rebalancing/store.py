import asyncio
from copy import deepcopy
from fastapi import HTTPException


class RecommendationStore:
    def __init__(self, clock):
        self.clock = clock
        self.lock = asyncio.Lock()
        self.records = {}

    def prune(self):
        now = self.clock.monotonic()
        self.records = {key: value for key, value in self.records.items() if value[0] > now}

    async def put(self, record, ttl):
        async with self.lock:
            self.prune()
            if len(self.records) >= 10000:
                raise HTTPException(503, "RECOMMENDATION_CAPACITY_REACHED")
            self.records[record["recommendation_id"]] = (self.clock.monotonic() + ttl, deepcopy(record))

    async def get(self, key):
        async with self.lock:
            self.prune()
            if key not in self.records:
                raise HTTPException(404, "RECOMMENDATION_MISSING_OR_EXPIRED")
            return deepcopy(self.records[key][1])
