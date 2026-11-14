"""Small PostgreSQL-backed snapshot repository for single-replica services.

The service stores remain responsible for validating and mutating their state.
This adapter only makes an already-serialized service snapshot durable.  It is
deliberately not a substitute for cross-replica locking or cross-service
transactions.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import random
from datetime import datetime
from enum import Enum
from threading import Timer
from uuid import UUID

import asyncpg
from pydantic import BaseModel


def encode_state(value):
    """Encode Python/Pydantic state into deterministic JSON-compatible data."""
    if isinstance(value, BaseModel):
        return {"__kind__": "model", "class": f"{value.__class__.__module__}:{value.__class__.__qualname__}",
                "value": value.model_dump(mode="json")}
    if isinstance(value, datetime):
        return {"__kind__": "datetime", "value": value.isoformat()}
    if isinstance(value, UUID):
        return {"__kind__": "uuid", "value": str(value)}
    if isinstance(value, Enum):
        return {"__kind__": "enum", "value": value.value}
    if isinstance(value, dict):
        return {"__kind__": "dict", "items": [[encode_state(key), encode_state(item)]
                                                   for key, item in value.items()]}
    if isinstance(value, set):
        return {"__kind__": "set", "items": [encode_state(item) for item in sorted(value, key=str)]}
    if isinstance(value, tuple):
        return {"__kind__": "tuple", "items": [encode_state(item) for item in value]}
    if isinstance(value, list):
        return [encode_state(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported durable state value: {type(value).__name__}")


def decode_state(value):
    """Restore data produced by :func:`encode_state`."""
    if isinstance(value, list):
        return [decode_state(item) for item in value]
    if not isinstance(value, dict) or "__kind__" not in value:
        return value
    kind = value["__kind__"]
    if kind == "dict":
        return {decode_state(key): decode_state(item) for key, item in value["items"]}
    if kind == "set":
        return {decode_state(item) for item in value["items"]}
    if kind == "tuple":
        return tuple(decode_state(item) for item in value["items"])
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    if kind == "uuid":
        return UUID(value["value"])
    if kind == "enum":
        return value["value"]
    if kind == "model":
        module_name, class_name = value["class"].split(":", 1)
        if not module_name.startswith("ev_fleet.contracts."):
            raise ValueError("Refusing to restore a model outside ev_fleet.contracts")
        model = importlib.import_module(module_name)
        cls = model
        for part in class_name.split("."):
            cls = getattr(cls, part)
        if not issubclass(cls, BaseModel):
            raise ValueError("Stored class is not a Pydantic model")
        return cls.model_validate(value["value"])
    raise ValueError(f"Unknown durable state kind: {kind}")


class SnapshotRepository:
    """One JSONB document per service namespace, written transactionally."""

    def __init__(self, namespace: str, url: str | None = None):
        self.namespace = namespace
        self.url = url
        self.pool = None
        # A failed/ambiguous commit must not leave RAM considered authoritative.
        # The owner must restart and reload the durable snapshot.
        self.faulted = False
        self._exit_on_uncertainty = False
        self._recovery_exit: Timer | None = None

    def enable_recovery_exit(self) -> None:
        """Opt in from a supervised service lifespan, never from store creation."""
        self._exit_on_uncertainty = True
        if self.faulted:
            self.mark_uncertain()

    def disable_recovery_exit(self) -> None:
        # Normal shutdown may disarm the policy, but cannot cancel an exit
        # already required by an ambiguous transaction.
        self._exit_on_uncertainty = False

    def mark_uncertain(self) -> None:
        """Fence all saves permanently; only a new process may reload authority."""
        self.faulted = True
        if self._exit_on_uncertainty and self._recovery_exit is None:
            logging.getLogger(__name__).critical(
                "PERSISTENCE_STATE_UNCERTAIN namespace=%s recovery=process_exit "
                "exit_code=75 delay_seconds=1; supervisor must restart and reload PostgreSQL",
                self.namespace)
            # A separate timer bounds the exit even if ASGI shutdown or pool
            # cleanup stalls. Do not run shutdown hooks that could save RAM.
            self._recovery_exit = Timer(1.0, os._exit, args=(75,))
            self._recovery_exit.daemon = True
            self._recovery_exit.start()

    @classmethod
    def from_env(cls, namespace: str):
        return cls(namespace, os.getenv("DATABASE_URL"))

    @property
    def enabled(self):
        return bool(self.url)

    @property
    def ready(self):
        return not self.faulted and (not self.enabled or self.pool is not None)

    async def start(self):
        if self.faulted:
            raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
        if not self.enabled or self.pool is not None:
            return
        attempts = min(30, max(1, int(os.getenv("DATABASE_CONNECT_ATTEMPTS", "30"))))
        delay = min(5.0, max(0.1, float(os.getenv("DATABASE_CONNECT_DELAY_SECONDS", "1"))))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 45.0
        last_error = None
        for attempt in range(1, attempts + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._connect(), timeout=min(5.0, remaining))
                return
            except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
                last_error = exc
                if self.pool is not None:
                    self.pool.terminate()
                    self.pool = None
                logging.getLogger(__name__).warning(
                    "postgres connection attempt %s/%s failed: %s", attempt, attempts, type(exc).__name__)
                if attempt < attempts:
                    ceiling = min(5.0, delay * 2 ** (attempt - 1))
                    await asyncio.sleep(min(
                        random.uniform(ceiling / 2, ceiling),
                        max(0.0, deadline - loop.time())))
        raise RuntimeError("PostgreSQL is configured but unavailable") from last_error

    async def _connect(self):
        self.pool = await asyncpg.create_pool(
            self.url, min_size=1, max_size=3, command_timeout=5, timeout=5)
        async with self.pool.acquire() as connection:
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS service_snapshots (
                    namespace TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    revision BIGINT NOT NULL DEFAULT 1,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            if self.namespace == "fleet":
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS telemetry_samples (
                        vehicle_id TEXT NOT NULL,
                        session_id UUID NOT NULL,
                        sequence BIGINT NOT NULL,
                        observed_at TIMESTAMPTZ NOT NULL,
                        received_at TIMESTAMPTZ NOT NULL,
                        payload JSONB NOT NULL,
                        PRIMARY KEY(vehicle_id, session_id, sequence)
                    )
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS telemetry_samples_vehicle_time_idx
                    ON telemetry_samples(vehicle_id, received_at DESC)
                """)

    async def load(self):
        if self.faulted:
            raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
        if not self.enabled:
            return None
        if self.pool is None:
            raise RuntimeError("Snapshot repository has not been started")
        async with self.pool.acquire(timeout=5) as connection:
            payload = await connection.fetchval(
                "SELECT payload FROM service_snapshots WHERE namespace = $1",
                self.namespace, timeout=5)
        if payload is None:
            return None
        if isinstance(payload, str):
            payload = json.loads(payload)
        return decode_state(payload)

    async def save(self, payload, *, telemetry_sample=None):
        if self.faulted:
            raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
        if not self.enabled:
            return
        if self.pool is None:
            raise RuntimeError("Snapshot repository has not been started")
        try:
            # Fleet telemetry is protected by its owner lock; Dispatch passes
            # an already-frozen snapshot. Keep their JSON encoding off the
            # request loop so command polls and lease heartbeats stay responsive.
            def encode():
                return json.dumps(encode_state(payload), allow_nan=False, separators=(",", ":"))
            encoded = (await asyncio.to_thread(encode)
                       if telemetry_sample is not None or self.namespace == "dispatch"
                       else encode())
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute("""
                    INSERT INTO service_snapshots(namespace, payload)
                    VALUES ($1, $2::jsonb)
                    ON CONFLICT(namespace) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        revision = service_snapshots.revision + 1,
                        updated_at = NOW()
                    """, self.namespace, encoded)
                    if telemetry_sample is not None:
                        # Snapshot and append-only history commit atomically.
                        await connection.execute("""
                        INSERT INTO telemetry_samples (
                            vehicle_id, session_id, sequence, observed_at,
                            received_at, payload
                        ) VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb)
                        ON CONFLICT(vehicle_id, session_id, sequence) DO NOTHING
                        """, telemetry_sample["vehicle_id"], str(telemetry_sample["session_id"]),
                        telemetry_sample["sequence"], telemetry_sample["observed_at"],
                        telemetry_sample["received_at"],
                        json.dumps(telemetry_sample["telemetry"], allow_nan=False))
        except (Exception, asyncio.CancelledError):
            # Cancellation during a DB transaction can be ambiguous too.
            # Even a timeout after COMMIT is ambiguous: never rollback only in
            # memory, nor continue accepting commands using possibly uncommitted
            # state. Readiness becomes false until a durable reload on restart.
            self.mark_uncertain()
            logging.getLogger(__name__).error(
                "snapshot persistence uncertain for namespace=%s; restart required",
                self.namespace)
            raise

    async def telemetry_history(self, vehicle_id, limit=100):
        if not self.enabled:
            return []
        if self.namespace != "fleet" or self.pool is None:
            raise RuntimeError("Telemetry history requires an active Fleet repository")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch("""
                SELECT vehicle_id, session_id::text AS session_id,
                       sequence, observed_at, received_at, payload
                FROM telemetry_samples WHERE vehicle_id = $1
                ORDER BY received_at DESC, sequence DESC LIMIT $2
            """, vehicle_id, limit)
        return [{"vehicle_id": row["vehicle_id"],
                 "session_id": row["session_id"], "sequence": row["sequence"],
                 "observed_at": row["observed_at"].isoformat(),
                 "received_at": row["received_at"].isoformat(),
                 "telemetry": json.loads(row["payload"]) if isinstance(row["payload"], str)
                     else row["payload"]} for row in reversed(rows)]

    async def close(self):
        if self.pool is not None:
            pool = self.pool
            try:
                await asyncio.wait_for(pool.close(), timeout=5)
            except asyncio.TimeoutError:
                pool.terminate()
                logging.getLogger(__name__).warning(
                    "postgres pool close timed out for namespace=%s; connections terminated",
                    self.namespace)
            finally:
                self.pool = None

    def status(self):
        if self.faulted:
            return "uncertain"
        return "postgresql" if self.enabled else "ephemeral"

