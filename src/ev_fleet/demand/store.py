import asyncio
from datetime import timedelta
from uuid import uuid4
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.demand import Zone, DemandUpdate


# Representative simulation points, not surveyed pickup bays or charger locations.
SINGAPORE_ZONES = [
    ("jurong-east", "Jurong East", 1.3331, 103.7422),
    ("woodlands", "Woodlands", 1.4360, 103.7860),
    ("bishan", "Bishan", 1.3509, 103.8485),
    ("punggol", "Punggol", 1.4052, 103.9023),
    ("tampines", "Tampines", 1.3545, 103.9452),
    ("cbd", "CBD", 1.2830, 103.8513),
]


class DemandStore:
    def __init__(self, clock=None, manual_hold_seconds=60, repository=None):
        self.clock = clock or Clock()
        self.lock = asyncio.Lock()
        self.zones = {}
        self.instance_id = str(uuid4())
        self.version = 0
        self.manual_hold_seconds = manual_hold_seconds
        self.repository = repository or SnapshotRepository.from_env("demand")

    async def start(self):
        await self.repository.start()
        payload = await self.repository.load()
        if payload:
            self.instance_id = payload["instance_id"]
            self.version = payload["version"]
            self.zones = payload["zones"]
            now, tick = self.clock.now(), self.clock.monotonic()
            for record in self.zones.values():
                hold_until = record["hold_until"]
                remaining = 0 if hold_until is None else max(0, (hold_until - now).total_seconds())
                record["hold_tick"] = tick + remaining if remaining else None
        else:
            await self.persist()

    async def close(self):
        await self.repository.close()

    async def persist(self):
        await self.repository.save({"instance_id": self.instance_id,
                                    "version": self.version,
                                    "zones": self.zones})

    def record(self, zone_id):
        if zone_id not in self.zones:
            raise HTTPException(404, "ZONE_NOT_FOUND")
        return self.zones[zone_id]

    async def create(self, zone):
        async with self.lock:
            if zone.zone_id in self.zones:
                raise HTTPException(409, "ZONE_EXISTS")
            record = {"zone": zone, "version": 1, "demand_version": 0,
                      "charging_version": 0, "demand": None, "charging": None,
                      "demand_source": "UNKNOWN", "hold_tick": None, "hold_until": None}
            self.zones[zone.zone_id] = record
            self.version += 1
            await self.persist()
            return self.view(record)

    def view(self, record):
        return {**record["zone"].model_dump(mode="json"),
                "instance_id": self.instance_id, "version": record["version"],
                **{field: record[field] for field in ("demand_version", "charging_version")},
                **{field: record[field].model_dump(mode="json", exclude={"expected_version"})
                   if record[field] else None for field in ("demand", "charging")}}

    async def update(self, zone_id, field, body):
        async with self.lock:
            record = self.record(zone_id)
            version_key = f"{field}_version"
            if body.expected_version != record[version_key]:
                raise HTTPException(409, "VERSION_CONFLICT")
            if (body.observed_at - self.clock.now()).total_seconds() > 5:
                raise HTTPException(422, "OBSERVATION_IN_FUTURE")
            if record[field] and body.observed_at < record[field].observed_at:
                raise HTTPException(409, "OBSERVATION_OUT_OF_ORDER")
            record[field] = body
            if field == "demand":
                record["demand_source"] = "MANUAL"
                record["hold_tick"] = self.clock.monotonic() + self.manual_hold_seconds
                record["hold_until"] = self.clock.now() + timedelta(seconds=self.manual_hold_seconds)
            record[version_key] += 1
            record["version"] += 1
            self.version += 1
            await self.persist()
            return self.view(record)

    async def get(self, zone_id):
        async with self.lock:
            return self.view(self.record(zone_id))

    async def snapshot(self):
        async with self.lock:
            return {"snapshot_at": self.clock.now().isoformat(), "instance_id": self.instance_id,
                    "zones": [self.view(record) for record in self.zones.values()]}

    def provenance(self, record):
        active = record["hold_tick"] is not None and self.clock.monotonic() < record["hold_tick"]
        return {"source": record["demand_source"],
                "manual_hold_until": record["hold_until"].isoformat() if active else None}

    async def composition_snapshot(self):
        # Fleet data is never stored here. This is a copy of demand-owned state only.
        async with self.lock:
            return {"snapshot_at": self.clock.now(), "instance_id": self.instance_id,
                    "zones": [self.view(record) for record in self.zones.values()],
                    "provenance": {key: self.provenance(record) for key, record in self.zones.items()}}

    async def simulation_tick(self, simulator):
        async with self.lock:
            if not simulator.settings.simulation_enabled:
                return 0
            now, tick = self.clock.now(), self.clock.monotonic()
            changed = 0
            for key in sorted(self.zones):
                record = self.zones[key]
                if record["hold_tick"] is not None and tick < record["hold_tick"]:
                    continue
                if record["demand"] and record["demand"].observed_at > now:
                    continue  # A clock correction must not overwrite a newer observation.
                current = record["demand"].desired_available_vehicles if record["demand"] else None
                value = simulator.next_value(current)
                record["demand"] = DemandUpdate(expected_version=record["demand_version"],
                    observed_at=now, desired_available_vehicles=value)
                record["demand_source"] = "SIMULATED"
                record["demand_version"] += 1
                record["version"] += 1
                changed += 1
            self.version += int(changed > 0)
            simulator.tick_count += 1
            simulator.last_tick_at = now
            if changed:
                await self.persist()
            return changed


async def seed(store):
    for key, name, lat, lon in SINGAPORE_ZONES:
        await store.create(Zone(zone_id=key, name=name,
                                position={"latitude": lat, "longitude": lon}))
