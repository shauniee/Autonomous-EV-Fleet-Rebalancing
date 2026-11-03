"""Single-replica facilities observations with durable simulated allocations."""
import asyncio
from uuid import uuid4
from copy import deepcopy
from fastapi import HTTPException
from ev_fleet.contracts.facilities import Facility
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.common.clock import Clock
from ev_fleet.contracts.facilities import FacilitiesSnapshot, FacilityFeed
from .catalog import CATALOGUE
from .datamall import FacilitiesSettings
from ev_fleet.common.parking import CHARGING_TARGET_SOC_PCT, DEFAULT_CHARGING_POWER_KW, MAX_FACILITY_ROAD_ACCESS_KM
from ev_fleet.common.travel import distance_km


class FacilitiesStore:
    def __init__(self, repository=None, settings=None, clock=None):
        self.lock = asyncio.Lock()
        self.holds = {}
        self.instance_id = str(uuid4())
        self.version = 0
        self.repository = repository or SnapshotRepository.from_env("facilities")
        self.settings = settings or FacilitiesSettings.from_env()
        self.clock = clock or Clock()
        self.catalogue = {site.facility_id: site for site in CATALOGUE} if self.settings.source == "MOCK" else {}
        self.feeds = {name: FacilityFeed(name=name, status=(
            "PENDING" if self.settings.account_key.get_secret_value() else "MISSING_KEY"))
            for name in ("parking", "charging")} if self.settings.source == "LTA_DATAMALL" else {}

    async def start(self):
        await self.repository.start()
        payload = await self.repository.load()
        if payload:
            self.instance_id = payload["instance_id"]
            self.version = payload["version"]
            self.holds = payload["holds"]
            if self.settings.source == "LTA_DATAMALL":
                # Live observations are an external cache, not durable state.
                # Re-fetch them after startup instead of decoding and repeatedly
                # saving a large tagged-JSON catalogue. Persist once here to
                # compact snapshots created by older versions of the service.
                await self.persist()
        else:
            await self.persist()

    async def close(self):
        await self.repository.close()

    async def persist(self):
        live_source = self.settings.source == "LTA_DATAMALL"
        await self.repository.save({"instance_id": self.instance_id,
                                    "version": self.version,
                                    "holds": self.holds,
                                    "catalogue": {} if live_source else self.catalogue,
                                    "feeds": {} if live_source else self.feeds})

    async def replace_feed(self, name, sites, observed_at):
        async with self.lock:
            kind = "PARKING" if name == "parking" else "CHARGING"
            self.catalogue = {key: site for key, site in self.catalogue.items()
                              if site.facility_type != kind}
            self.catalogue.update({site.facility_id: site for site in sites})
            self.feeds[name] = FacilityFeed(name=name, status="FRESH", last_success_at=observed_at)
            self.version += 1
            await self.persist()

    async def feed_failed(self, name, code):
        async with self.lock:
            self.feeds[name] = FacilityFeed(name=name, status="ERROR", error_code=code,
                                          last_success_at=self.feeds[name].last_success_at)
            self.version += 1
            await self.persist()

    def feed_views(self):
        views = []
        for name, feed in self.feeds.items():
            status = feed.status
            if (feed.last_success_at and status != "MISSING_KEY" and
                    (self.clock.now() - feed.last_success_at).total_seconds()
                    >= getattr(self.settings, f"{name}_max_age_seconds")):
                status = "STALE"
            views.append(feed.model_copy(update={"status": status}))
        return views

    async def view(self, zone_id=None):
        # Reads contain no await and therefore run atomically on the service's
        # single asyncio event loop. Do not queue them behind a potentially
        # slow PostgreSQL save performed by a feed or hold mutation.
        return FacilitiesSnapshot(
            generated_at=self.clock.now(), data_source=self.settings.source,
            facilities=[self.effective(site) for site in self.catalogue.values()
                        if zone_id is None or site.zone_id == zone_id], feeds=self.feed_views())

    def effective(self, facility):
        claims = [h for h in self.holds.values() if h["facility_id"] == facility.facility_id
                  and h["status"] in ("RESERVED", "OCCUPIED")]
        parking_available = max(0, facility.parking_available - len(claims))
        charging_available = max(0, min(parking_available, facility.charging_available
                                       - sum(h["charging_allocated"] for h in claims)))
        stale = not facility.is_fresh(self.clock.now())
        if facility.source == "LTA_DATAMALL" and not self.settings.account_key.get_secret_value():
            stale = True
        if stale:
            parking_available = charging_available = 0
        return Facility.model_validate({**facility.model_dump(mode="json"),
            "parking_available": parking_available, "charging_available": charging_available,
            "status": "UNKNOWN" if stale else facility.status})

    async def snapshot(self, zone_id=None):
        return [self.effective(site) for site in self.catalogue.values()
                if zone_id is None or site.zone_id == zone_id]

    async def get(self, facility_id):
        site = self.catalogue.get(facility_id)
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
            site = self.catalogue.get(facility_id)
            if site is None:
                raise HTTPException(404, "FACILITY_NOT_FOUND")
            if site.zone_id != body.expected_zone_id or site.position != body.expected_position:
                raise HTTPException(409, "PARKING_FACILITY_CHANGED")
            if (body.access_position is not None and
                    distance_km(body.access_position.model_dump(), site.position.model_dump())
                    > MAX_FACILITY_ROAD_ACCESS_KM):
                raise HTTPException(409, "PARKING_ROAD_ACCESS_TOO_FAR")
            if site.status != "OPEN":
                raise HTTPException(409, "PARKING_CLOSED")
            if site.facility_type == "CHARGING" and not body.requires_charging:
                raise HTTPException(409, "CHARGING_NOT_REQUIRED")
            # An OCCUPIED hold is the vehicle's current bay. Keep it allocated
            # while reserving the next destination so a failed relocation does
            # not incorrectly free the bay the vehicle still occupies. Only a
            # second pending destination is a conflicting assignment.
            if any(h["receipt"]["vehicle_id"] == body.vehicle_id
                   and h["status"] == "RESERVED" for h in self.holds.values()):
                raise HTTPException(409, "VEHICLE_ALREADY_ASSIGNED_PARKING")
            available = self.effective(site)
            if available.status == "UNKNOWN":
                raise HTTPException(409, "FACILITY_DATA_STALE")
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
                               "facility": site,
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
        entry = self.holds.get(str(dispatch_id))
        if not entry:
            raise HTTPException(404, "PARKING_HOLD_NOT_FOUND")
        return deepcopy(entry["receipt"])

    async def vehicle_parking(self, vehicle_id):
        entries = [entry for entry in self.holds.values()
                   if entry["receipt"]["vehicle_id"] == vehicle_id]
        occupied = next((entry for entry in entries if entry["status"] == "OCCUPIED"), None)
        parking = None
        if occupied:
            site = occupied.get("facility") or self.catalogue.get(occupied["facility_id"])
            if site:
                parking = {"facility_id": site.facility_id, "name": site.name,
                    "position": occupied["request"].access_position or site.position,
                    "zone_id": site.zone_id, "source": site.source,
                    "charging_allocated": occupied["charging_allocated"],
                    "charging_power_kw": site.charging_power_kw or DEFAULT_CHARGING_POWER_KW,
                    "charging_target_soc_pct": CHARGING_TARGET_SOC_PCT}
        return {"vehicle_id": vehicle_id, "parking": parking,
                "pending": any(entry["status"] == "RESERVED" for entry in entries)}
