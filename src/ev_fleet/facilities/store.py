"""Single-process mock parking/charging allocation registry (not durable)."""
import asyncio
from uuid import uuid4
from copy import deepcopy
from fastapi import HTTPException
from ev_fleet.contracts.facilities import Facility
from ev_fleet.common.persistence import SnapshotRepository
from .catalog import CATALOGUE, BY_ID


class FacilitiesStore:
    def __init__(self, repository=None):
        self.lock = asyncio.Lock()
        self.holds = {}
        self.instance_id = str(uuid4())
        self.version = 0
        self.repository = repository or SnapshotRepository.from_env("facilities")

    async def start(self):
        await self.repository.start()
        payload = await self.repository.load()
        if payload:
            self.instance_id = payload["instance_id"]
            self.version = payload["version"]
            self.holds = payload["holds"]
        else:
            await self.persist()

    async def close(self):
        await self.repository.close()

    async def persist(self):
        await self.repository.save({"instance_id": self.instance_id,
                                    "version": self.version,
                                    "holds": self.holds})

    def effective(self, facility):
        claims = [h for h in self.holds.values() if h["facility_id"] == facility.facility_id
                  and h["status"] in ("RESERVED", "OCCUPIED")]
        parking_available = facility.parking_available - len(claims)
        charging_available = facility.charging_available - sum(h["charging_allocated"] for h in claims)
        return Facility.model_validate({**facility.model_dump(mode="json"),
            "parking_available": parking_available, "charging_available": charging_available})

    async def snapshot(self, zone_id=None):
        async with self.lock:
            return [self.effective(site) for site in CATALOGUE
                    if zone_id is None or site.zone_id == zone_id]

    async def get(self, facility_id):
        async with self.lock:
            site = BY_ID.get(facility_id)
            if not site:
                raise HTTPException(404, "FACILITY_NOT_FOUND")
            return self.effective(site)

    async def reserve(self, facility_id, body):
        async with self.lock:
            key = str(body.dispatch_id)
            entry = self.holds.get(key)
            if entry:
                if entry["facility_id"] != facility_id or entry["request"] != body:
                    raise HTTPException(409, "PARKING_HOLD_ID_CONFLICT")
                return deepcopy(entry["receipt"])
            site = BY_ID.get(facility_id)
            if site is None:
                raise HTTPException(404, "FACILITY_NOT_FOUND")
            if site.zone_id != body.expected_zone_id or site.position != body.expected_position:
                raise HTTPException(409, "PARKING_FACILITY_CHANGED")
            if site.status != "OPEN":
                raise HTTPException(409, "PARKING_CLOSED")
            # An OCCUPIED hold is the vehicle's current bay. Keep it allocated
            # while reserving the next destination so a failed relocation does
            # not incorrectly free the bay the vehicle still occupies. Only a
            # second pending destination is a conflicting assignment.
            if any(h["receipt"]["vehicle_id"] == body.vehicle_id
                   and h["status"] == "RESERVED" for h in self.holds.values()):
                raise HTTPException(409, "VEHICLE_ALREADY_ASSIGNED_PARKING")
            available = self.effective(site)
            if available.parking_available < 1:
                raise HTTPException(409, "PARKING_FULL")
            if body.requires_charging and available.charging_available < 1:
                raise HTTPException(409, "CHARGING_FULL")
            # Use ordinary spaces first; charging places are part of parking capacity.
            charging = body.requires_charging or (
                available.parking_available == available.charging_available
                and available.charging_available > 0)
            receipt = {"dispatch_id": key, "vehicle_id": body.vehicle_id,
                       "facility_id": facility_id, "requires_charging": body.requires_charging,
                       "charging_allocated": charging, "status": "RESERVED"}
            self.version += 1
            self.holds[key] = {"facility_id": facility_id, "request": body,
                               "receipt": receipt, "charging_allocated": charging,
                               "status": "RESERVED"}
            await self.persist()
            return deepcopy(receipt)

    async def change(self, dispatch_id, status):
        async with self.lock:
            entry = self.holds.get(str(dispatch_id))
            if not entry:
                raise HTTPException(404, "PARKING_HOLD_NOT_FOUND")
            if entry["status"] == status:
                return deepcopy(entry["receipt"])
            if entry["status"] != "RESERVED":
                raise HTTPException(409, "PARKING_HOLD_TERMINAL")
            if status == "OCCUPIED":
                vehicle_id = entry["receipt"]["vehicle_id"]
                for other in self.holds.values():
                    if (other is not entry and other["status"] == "OCCUPIED"
                            and other["receipt"]["vehicle_id"] == vehicle_id):
                        other["status"] = "RELEASED"
                        other["receipt"]["status"] = "RELEASED"
            entry["status"] = status
            entry["receipt"]["status"] = status
            self.version += 1
            await self.persist()
            return deepcopy(entry["receipt"])

    async def get_hold(self, dispatch_id):
        async with self.lock:
            entry = self.holds.get(str(dispatch_id))
            if not entry:
                raise HTTPException(404, "PARKING_HOLD_NOT_FOUND")
            return deepcopy(entry["receipt"])
