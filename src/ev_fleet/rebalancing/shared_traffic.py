"""Shared, versioned LTA traffic cache.

Only the dedicated collector fetches LTA. Rebalancing periodically reads small
Redis metadata and materialises each feed only when its content digest changes.
Redis failures, missing blobs and expired observations are NEVER clear roads.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import zlib

from redis.asyncio import Redis
from redis.exceptions import RedisError

from .traffic import FEEDS, TrafficDataError, TrafficMonitor

LOGGER = logging.getLogger(__name__)
META_KEY = "ev-fleet:traffic:v1:meta"
DATA_KEY_PREFIX = "ev-fleet:traffic:v1:data"
MAX_ENCODED_BYTES = 12 * 1024 * 1024
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024


def utcnow():
    return datetime.now(timezone.utc)


def redis_client(url=None):
    url = url or os.getenv("REDIS_URL")
    if not url:
        raise ValueError("REDIS_URL_REQUIRED")
    return Redis.from_url(
        url, decode_responses=False, socket_connect_timeout=0.5,
        socket_timeout=1.0, retry_on_timeout=False,
    )


def data_key(name, digest):
    return f"{DATA_KEY_PREFIX}:{name}:{digest}"


def _parse_time(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _encode_rows(rows):
    raw = json.dumps(rows, separators=(",", ":"), sort_keys=True,
                     allow_nan=False).encode("utf-8")
    if len(raw) > MAX_ENCODED_BYTES:
        raise ValueError("TRAFFIC_PAYLOAD_TOO_LARGE")
    return hashlib.sha256(raw).hexdigest(), zlib.compress(raw, level=3)


def _decode_rows(blob, digest):
    if not isinstance(blob, bytes) or len(blob) > MAX_COMPRESSED_BYTES:
        raise ValueError("TRAFFIC_CACHE_BLOB_INVALID")
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(blob, MAX_ENCODED_BYTES + 1)
    except zlib.error as exc:
        raise ValueError("TRAFFIC_CACHE_BLOB_INVALID") from exc
    if (len(raw) > MAX_ENCODED_BYTES or decompressor.unconsumed_tail
            or not decompressor.eof or decompressor.unused_data):
        raise ValueError("TRAFFIC_CACHE_BLOB_INVALID")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("TRAFFIC_CACHE_HASH_MISMATCH")
    rows = json.loads(raw)
    if not isinstance(rows, list) or len(rows) > 30000 or any(
            not isinstance(row, dict) for row in rows):
        raise ValueError("TRAFFIC_CACHE_SCHEMA_INVALID")
    return rows


class TrafficCachePublisher:
    """Single-writer, atomic per-feed publication; valid data before metadata."""

    def __init__(self, redis):
        self.redis = redis

    async def success(self, name, rows, observed_at):
        if name not in FEEDS:
            raise ValueError("UNKNOWN_TRAFFIC_FEED")
        digest, encoded = _encode_rows(rows)
        if len(encoded) > MAX_COMPRESSED_BYTES:
            raise ValueError("TRAFFIC_PAYLOAD_TOO_LARGE")
        max_age = FEEDS[name][2]
        metadata = {
            "digest": digest, "observed_at": observed_at.isoformat(),
            "count": len(rows), "error": None,
        }
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(data_key(name, digest), encoded, ex=max_age + 300)
            pipe.hset(META_KEY, name, json.dumps(metadata, separators=(",", ":")))
            await pipe.execute()
        return digest

    async def error(self, name, reason):
        if name not in FEEDS:
            raise ValueError("UNKNOWN_TRAFFIC_FEED")
        old = await self.redis.hget(META_KEY, name)
        try:
            previous = json.loads(old) if old else {}
        except (ValueError, TypeError):
            previous = {}
        metadata = {
            "digest": previous.get("digest"),
            "observed_at": previous.get("observed_at"),
            "count": previous.get("count", 0),
            "error": reason,
        }
        await self.redis.hset(META_KEY, name, json.dumps(metadata, separators=(",", ":")))


class RedisTrafficReader:
    """Local memory is served synchronously; Redis I/O is outside API requests."""

    def __init__(self, redis=None, source=None, clock=None):
        self.source = (source or os.getenv("TRAFFIC_SOURCE", "DISABLED")).upper()
        self.clock = clock or utcnow
        self.redis = redis
        self.entries = {}
        self.redis_error = "CACHE_PENDING"

    async def refresh(self):
        if self.source != "LTA_DATAMALL":
            return
        if self.redis is None:
            self.entries = {}
            self.redis_error = "CACHE_UNAVAILABLE"
            return
        try:
            all_meta = await self.redis.hgetall(META_KEY)
            previous = self.entries
            updated = {}
            for name in FEEDS:
                value = all_meta.get(name.encode()) or all_meta.get(name)
                if not value:
                    updated[name] = {"error": "CACHE_PENDING", "rows": [],
                                     "observed_at": None, "digest": None}
                    continue
                try:
                    meta = json.loads(value)
                    digest = meta.get("digest")
                    observed_at = _parse_time(meta.get("observed_at"))
                    if not digest or not observed_at:
                        updated[name] = {"error": meta.get("error") or "CACHE_PENDING",
                                         "rows": [], "observed_at": None, "digest": None}
                        continue
                    if not isinstance(digest, str) or len(digest) != 64:
                        raise ValueError("INVALID_DIGEST")
                    if previous.get(name, {}).get("digest") == digest:
                        rows = previous[name]["rows"]
                    else:
                        blob = await self.redis.get(data_key(name, digest))
                        rows = _decode_rows(blob, digest)
                    updated[name] = {
                        "error": meta.get("error"), "digest": digest,
                        "observed_at": observed_at, "rows": rows,
                    }
                except (ValueError, TypeError, KeyError, zlib.error):
                    updated[name] = {"error": "CACHE_INVALID", "rows": [],
                                     "observed_at": None, "digest": None}
            self.entries = updated
            self.redis_error = None
        except (RedisError, OSError):
            # Do not accidentally serve a formerly cached all-clear on a
            # disconnected Redis authority. Rebalancing remains available.
            self.entries = {}
            self.redis_error = "CACHE_UNAVAILABLE"

    def cached_snapshot(self):
        now = self.clock()
        if self.source != "LTA_DATAMALL":
            return {
                "source": "DISABLED", "generated_at": now.isoformat(),
                "status": "DISABLED", "feeds": {},
                "speeds": [], "incidents": [], "roadworks": [],
            }
        feeds = {}
        data = {}
        for name, (_, _, max_age) in FEEDS.items():
            entry = self.entries.get(name, {})
            observed = entry.get("observed_at")
            age = (now - observed).total_seconds() if observed else None
            fresh = (age is not None and 0 <= age < max_age and
                     self.redis_error is None)
            failure = self.redis_error or entry.get("error")
            feeds[name] = {
                "status": "FRESH" if fresh else "STALE" if age is not None else
                          (failure or "CACHE_PENDING"),
                "observed_at": observed.isoformat() if observed else None,
                "count": len(entry.get("rows", [])) if fresh else 0,
                "error": failure,
            }
            data[name] = entry.get("rows", []) if fresh else []
        return {
            "source": "LTA_DATAMALL", "generated_at": now.isoformat(),
            "status": ("FRESH" if all(f["status"] == "FRESH"
                                    for f in feeds.values()) else "PARTIAL"),
            "feeds": feeds, **data,
        }

    async def refresh_loop(self):
        while True:
            await self.refresh()
            await asyncio.sleep(10)

    async def aclose(self):
        if self.redis is not None:
            await self.redis.aclose()


async def collect_feed(monitor, publisher, name, *, sleeper=asyncio.sleep):
    """Independent feed loop: a slow Speed Bands request never blocks incidents."""
    refresh_seconds = FEEDS[name][1]
    while True:
        try:
            rows = await monitor._fetch(name)
            await publisher.success(name, rows, utcnow())
            LOGGER.info("traffic feed=%s collected=%d", name, len(rows))
        except TrafficDataError as exc:
            try:
                await publisher.error(name, str(exc))
            except (RedisError, OSError):
                LOGGER.warning("traffic feed=%s cache write unavailable", name)
            LOGGER.warning("traffic feed=%s failed=%s", name, str(exc))
        except (RedisError, OSError, ValueError):
            LOGGER.warning("traffic feed=%s cache unavailable or invalid", name)
        await sleeper(refresh_seconds)


async def run_collector():
    if os.getenv("TRAFFIC_SOURCE", "DISABLED").upper() != "LTA_DATAMALL":
        LOGGER.info("traffic collector idle: live LTA traffic disabled")
        while True:
            await asyncio.sleep(60)
    if not os.getenv("LTA_DATAMALL_ACCOUNT_KEY", "").strip():
        LOGGER.warning("traffic collector cannot poll: LTA key missing")
        while True:
            await asyncio.sleep(60)
    client = redis_client()
    try:
        import httpx
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(8.0),
                limits=httpx.Limits(max_connections=5)) as http:
            monitor = TrafficMonitor(http, source="LTA_DATAMALL")
            writer = TrafficCachePublisher(client)
            await asyncio.gather(*(collect_feed(monitor, writer, name)
                                   for name in FEEDS))
    finally:
        await client.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_collector())
