"""Atomic per-record storage for the existing single-writer service stores.

The snapshot row contains metadata and an ordered record-key manifest. Individual
records and optional telemetry history commit in the same PostgreSQL transaction.
Legacy snapshots are promoted on the first successful save, never during load.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable

from .persistence import SnapshotRepository, decode_state, encode_state


class RecordSnapshotRepository(SnapshotRepository):
    def __init__(self, namespace: str, records_key: str, url: str | None = None):
        super().__init__(namespace, url)
        self.records_key = records_key
        self._partitioned = False
        self._record_keys: set[str] = set()

    @property
    def requires_promotion(self):
        return self.enabled and not self._partitioned

    @classmethod
    def from_env(cls, namespace: str, records_key: str | None = None):
        if records_key is None:
            raise ValueError("A record field is required for partitioned storage")
        return cls(namespace, records_key, os.getenv("DATABASE_URL"))

    async def _connect(self):
        await super()._connect()
        pool = self.pool
        if pool is None:
            raise RuntimeError("Snapshot repository has not been started")
        async with pool.acquire() as connection:
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_records (
                    namespace TEXT NOT NULL REFERENCES service_snapshots(namespace) ON DELETE CASCADE,
                    record_key TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    PRIMARY KEY (namespace, record_key)
                )
            """)

    @staticmethod
    def _json(value):
        return json.loads(value) if isinstance(value, str) else value

    async def load(self):
        if self.faulted:
            raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
        if not self.enabled:
            return None
        if self.pool is None:
            raise RuntimeError("Snapshot repository has not been started")
        async with self.pool.acquire(timeout=5) as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                raw = await connection.fetchval(
                    "SELECT payload FROM service_snapshots WHERE namespace=$1", self.namespace)
                if raw is None:
                    return None
                header = self._json(raw)
                if "__partitioned_snapshot__" not in header:
                    self._partitioned = False
                    return decode_state(header)
                if header["__partitioned_snapshot__"] != 1:
                    raise RuntimeError("SNAPSHOT_FORMAT_UNSUPPORTED")
                if header.get("records_key") != self.records_key:
                    raise RuntimeError("SNAPSHOT_RECORD_FIELD_MISMATCH")
                keys = header["record_keys"]
                if (not isinstance(keys, list) or any(not isinstance(key, str) for key in keys)
                        or len(keys) != len(set(keys))):
                    raise RuntimeError("SNAPSHOT_RECORD_MANIFEST_INVALID")
                rows = await connection.fetch(
                    "SELECT record_key,payload FROM snapshot_records WHERE namespace=$1", self.namespace)
                records = {row["record_key"]: decode_state(self._json(row["payload"])) for row in rows}
                if set(records) != set(keys):
                    raise RuntimeError("SNAPSHOT_RECORD_SET_MISMATCH")
                result = decode_state(header["state"])
                if not isinstance(result, dict) or self.records_key in result:
                    raise RuntimeError("SNAPSHOT_METADATA_INVALID")
                result[self.records_key] = {key: records[key] for key in keys}
        self._partitioned = True
        self._record_keys = set(keys)
        return result

    async def save(self, payload, *, telemetry_sample=None, telemetry_samples=None,
                   changed_keys: Iterable[str] | None = None):
        if self.faulted:
            raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
        if not self.enabled:
            return
        if self.pool is None:
            raise RuntimeError("Snapshot repository has not been started")
        try:
            records = payload[self.records_key]
            keys = list(records)
            if any(not isinstance(key, str) for key in keys):
                raise ValueError("Snapshot record keys must be strings")
            # Promotion must import EVERY legacy record, regardless of the hint.
            changed = (set(keys) if not self._partitioned or changed_keys is None
                       else set(changed_keys) | (set(keys) - self._record_keys))
            metadata = {key: value for key, value in payload.items() if key != self.records_key}

            def encode():
                header = json.dumps({"__partitioned_snapshot__": 1,
                    "records_key": self.records_key, "record_keys": keys,
                    "state": encode_state(metadata)}, allow_nan=False, separators=(",", ":"))
                updates = json.dumps([
                    {"record_key": key, "payload": encode_state(records[key])}
                    for key in keys if key in changed], allow_nan=False, separators=(",", ":"))
                return header, updates

            header, updates = await asyncio.to_thread(encode)
            query = """
                WITH written_header AS (
                    INSERT INTO service_snapshots(namespace,payload) VALUES ($1,$2::jsonb)
                    ON CONFLICT(namespace) DO UPDATE SET payload=EXCLUDED.payload,
                        revision=service_snapshots.revision+1, updated_at=NOW()
                    RETURNING namespace
                ), written_records AS (
                    INSERT INTO snapshot_records(namespace,record_key,payload)
                    SELECT h.namespace,r.record_key,r.payload FROM written_header h
                    CROSS JOIN jsonb_to_recordset($3::jsonb) AS r(record_key TEXT,payload JSONB)
                    ON CONFLICT(namespace,record_key) DO UPDATE SET payload=EXCLUDED.payload
                    RETURNING record_key
                ), removed_records AS (
                    DELETE FROM snapshot_records
                    WHERE namespace=$1 AND record_key <> ALL($4::text[])
                    RETURNING record_key
                )
            """
            args = [self.namespace, header, updates, keys]
            if telemetry_sample is not None and telemetry_samples is not None:
                raise ValueError("Supply one telemetry sample or a group, not both")
            samples = ([telemetry_sample] if telemetry_sample is not None else telemetry_samples) or []
            if samples:
                query += """, written_history AS (
                    INSERT INTO telemetry_samples(vehicle_id,session_id,sequence,observed_at,received_at,payload)
                    SELECT h.vehicle_id,h.session_id,h.sequence,h.observed_at,h.received_at,h.payload
                    FROM jsonb_to_recordset($5::jsonb) AS h(vehicle_id TEXT,session_id UUID,
                        sequence BIGINT,observed_at TIMESTAMPTZ,received_at TIMESTAMPTZ,payload JSONB)
                    ON CONFLICT(vehicle_id,session_id,sequence) DO NOTHING
                    RETURNING vehicle_id
                )"""
                args.append(json.dumps([
                    {"vehicle_id": sample["vehicle_id"], "session_id": str(sample["session_id"]),
                     "sequence": sample["sequence"], "observed_at": sample["observed_at"].isoformat(),
                     "received_at": sample["received_at"].isoformat(), "payload": sample["telemetry"]}
                    for sample in samples], allow_nan=False, separators=(",", ":")))
            query += " SELECT namespace FROM written_header"
            # One statement is one implicit transaction, including deferred
            # constraint checks at commit. Its response is the durability barrier.
            async with self.pool.acquire() as connection:
                await connection.execute(query, *args)
            self._partitioned = True
            self._record_keys = set(keys)
        except (Exception, asyncio.CancelledError):
            self.mark_uncertain()
            raise
