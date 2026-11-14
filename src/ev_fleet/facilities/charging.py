"""Allocation-bound human cable confirmations; no physical charger integration."""
import asyncio
from copy import deepcopy
import os

import httpx
from fastapi import HTTPException

from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.facilities import ChargingCableUpdate, VehicleParking
from ev_fleet.contracts.vehicles import VehicleState


class ChargingHandoffs:
    def __init__(self, store, client=None):
        self.store = store
        self.client = client
        self.owns_client = client is None
        self.fleet_url = os.getenv("FLEET_URL", "http://fleet:8000").rstrip("/")

    async def close(self):
        if self.owns_client and self.client is not None:
            await self.client.aclose()

    def entry(self, vehicle_id, body):
        store = self.store
        if not store.repository.ready:
            raise HTTPException(503, "CHARGING_HANDOFF_UNAVAILABLE")
        entry = store.holds.get(str(body.dispatch_id))
        if entry is None or entry["receipt"]["vehicle_id"] != vehicle_id:
            raise HTTPException(409, "CHARGING_SESSION_CHANGED")
        return entry

    @staticmethod
    def retry(entry, body):
        previous = entry.get("cable_requests", {}).get(str(body.request_id))
        if previous:
            if previous["request"] != body.model_dump(mode="json"):
                raise HTTPException(409, "CHARGING_REQUEST_ID_CONFLICT")
            return deepcopy(previous["receipt"])
        return None

    def validate_allocation(self, vehicle_id, entry, body):
        parking = VehicleParking.model_validate(self.store._vehicle_parking(vehicle_id))
        site = parking.parking
        if (entry["status"] != "OCCUPIED" or not entry["charging_allocated"]
                or parking.pending or site is None
                or str(site.charging_session_id) != str(body.dispatch_id)):
            raise HTTPException(409, "CHARGING_ALLOCATION_NOT_READY")
        required = "AWAITING_CONNECTION" if body.action == "CONNECT" else "CONNECTED"
        if site.cable_status != required:
            raise HTTPException(409, "CHARGING_CABLE_STATE_CHANGED")
        return site

    async def confirm(self, vehicle_id: str, body: ChargingCableUpdate):
        store = self.store
        async with store.lock:
            entry = self.entry(vehicle_id, body)
            retry = self.retry(entry, body)
            if retry is not None:
                return retry
            self.validate_allocation(vehicle_id, entry, body)
        # No Facilities lock is held while consulting Fleet. The allocation
        # and action are checked again after the network round trip.
        requested_tick = store.clock.monotonic()
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=2)
        try:
            response = await asyncio.wait_for(self.client.get(
                f"{self.fleet_url}/api/v1/vehicles/{vehicle_id}"), timeout=2)
            response.raise_for_status()
            vehicle = VehicleState.model_validate(response.json())
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
            raise HTTPException(503, "CHARGING_FLEET_EVIDENCE_UNAVAILABLE") from exc
        async with store.lock:
            entry = self.entry(vehicle_id, body)
            retry = self.retry(entry, body)
            if retry is not None:
                return retry
            site = self.validate_allocation(vehicle_id, entry, body)
            telemetry = vehicle.telemetry
            age = ((store.clock.now() - telemetry.observed_at).total_seconds()
                   if telemetry else float("inf"))
            expected = "AWAITING_CONNECTION" if body.action == "CONNECT" else "COMPLETE"
            if (not 0 <= store.clock.monotonic() - requested_tick < 2
                    or vehicle.vehicle_id != vehicle_id or vehicle.stale
                    or vehicle.connectivity != "ONLINE" or not -5 <= age < 15
                    or vehicle.active_claim or vehicle.active_rental or vehicle.active_charging_task
                    or telemetry is None or telemetry.relocation_state != "IDLE"
                    or telemetry.active_dispatch_id is not None or telemetry.speed_kph != 0
                    or telemetry.parked_facility_id != site.facility_id
                    or telemetry.charging_state != expected
                    or distance_km(telemetry.position.model_dump(), site.position.model_dump()) > .05
                    or body.action == "DISCONNECT" and telemetry.soc_pct < site.charging_target_soc_pct):
                raise HTTPException(409, "CHARGING_FRESH_STOPPED_EV_REQUIRED")
            # Stage privately: ordinary simulator parking reads must not see a
            # connection until its durable confirmation has committed.
            staged = deepcopy(entry)
            holds = {**store.holds, str(body.dispatch_id): staged}
            cable = staged.setdefault("cable", {})
            cable["status"] = "CONNECTED" if body.action == "CONNECT" else "DISCONNECTED"
            prefix = "cable_connected" if body.action == "CONNECT" else "cable_disconnected"
            cable[prefix + "_by"] = body.staff_name
            cable[prefix + "_at"] = store.clock.now()
            entries = [item for item in holds.values()
                       if item["receipt"]["vehicle_id"] == vehicle_id]
            receipt = VehicleParking.model_validate(
                store._parking_view(vehicle_id, entries)).model_dump(mode="json")
            staged.setdefault("cable_requests", {})[str(body.request_id)] = {
                "request": body.model_dump(mode="json"), "receipt": receipt}
            version = store.version + 1
            await store.persist(holds=holds, version=version)
            store.holds, store.version = holds, version
            return receipt
