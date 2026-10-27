"""Fail-open, short-lived Redis cache for display-only snapshots.

Dispatch, ranking, vehicle claims, parking allocation and rental mutations
must NEVER depend on the content of this cache.
"""
import json
import logging
import os
from time import monotonic

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError


class DashboardCache:
    def __init__(self, client=None, ttl_seconds=1, namespace="dashboard", circuit_seconds=5):
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.namespace = namespace
        self.circuit_seconds = circuit_seconds
        self.disabled_until = 0.0

    @classmethod
    def from_env(cls, service, instance_id, ttl_seconds):
        url = os.getenv("REDIS_URL")
        if not url or os.getenv("DASHBOARD_CACHE_ENABLED", "true").lower() not in ("1", "true", "yes"):
            return cls(ttl_seconds=ttl_seconds, namespace=f"ev-fleet:{service}:{instance_id}")
        client = Redis.from_url(url, decode_responses=True,
                                socket_connect_timeout=0.15, socket_timeout=0.15,
                                retry_on_timeout=False)
        return cls(client=client, ttl_seconds=ttl_seconds,
                   namespace=f"ev-fleet:dashboard:v1:{service}:{instance_id}")

    async def get_or_compute(self, key, producer, model):
        """Return (validated JSON-compatible snapshot, HIT|MISS|BYPASS)."""
        k = f"{self.namespace}:{key}"
        enabled = self.client is not None and monotonic() >= self.disabled_until
        if enabled:
            try:
                raw = await self.client.get(k)
                if raw is not None:
                    return model.model_validate(json.loads(raw)).model_dump(mode="json"), "HIT"
            except (RedisError, OSError, ValueError, ValidationError) as exc:
                self.disabled_until = monotonic() + self.circuit_seconds
                logging.getLogger(__name__).warning("dashboard cache read bypass: %s", type(exc).__name__)
                enabled = False
        payload = model.model_validate(await producer()).model_dump(mode="json")
        if enabled:
            try:
                await self.client.set(k, json.dumps(payload, allow_nan=False), ex=self.ttl_seconds)
                return payload, "MISS"
            except (RedisError, OSError, ValueError) as exc:
                self.disabled_until = monotonic() + self.circuit_seconds
                logging.getLogger(__name__).warning("dashboard cache write bypass: %s", type(exc).__name__)
        return payload, "BYPASS"

    async def aclose(self):
        if self.client is not None:
            await self.client.aclose()
