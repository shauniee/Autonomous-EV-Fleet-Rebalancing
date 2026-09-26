import asyncio
from uuid import uuid4
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.contracts.demand import Zone


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
    def __init__(self, clock=None):
        self.clock = clock or Clock()
        self.lock = asyncio.Lock()
        self.zones = {}
        self.instance_id = str(uuid4())

    def record(self, zone_id):
        if zone_id not in self.zones:
            raise HTTPException(404, "ZONE_NOT_FOUND")
        return self.zones[zone_id]

    async def create(self, zone):
        async with self.lock:
            if zone.zone_id in self.zones:
                raise HTTPException(409, "ZONE_EXISTS")
            record = {"zone": zone, "version": 1, "demand_version": 0,
                      "charging_version": 0, "demand": None, "charging": None}
            self.zones[zone.zone_id] = record
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
            record[version_key] += 1
            record["version"] += 1
            return self.view(record)

    async def get(self, zone_id):
        async with self.lock:
            return self.view(self.record(zone_id))

    async def snapshot(self):
        async with self.lock:
            return {"snapshot_at": self.clock.now().isoformat(), "instance_id": self.instance_id,
                    "zones": [self.view(record) for record in self.zones.values()]}


async def seed(store):
    for key, name, lat, lon in SINGAPORE_ZONES:
        await store.create(Zone(zone_id=key, name=name,
                                position={"latitude": lat, "longitude": lon}))
