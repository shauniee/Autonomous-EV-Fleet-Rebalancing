import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from ev_fleet.common.persistence import decode_state, encode_state
from ev_fleet.contracts.dispatch import DispatchApproval
from ev_fleet.contracts.facilities import ParkingHoldRequest
from ev_fleet.contracts.vehicles import Position
from ev_fleet.dispatch.service import DispatchService
from ev_fleet.facilities.catalog import BY_ID
from ev_fleet.facilities.store import FacilitiesStore


class MemoryRepository:
    enabled = True
    ready = True

    def __init__(self):
        self.payload = None

    async def start(self):
        return None

    async def load(self):
        return deepcopy(self.payload)

    async def save(self, payload):
        # Exercise the exact tagged JSON round trip used by PostgreSQL JSONB.
        encoded = json.loads(json.dumps(encode_state(payload), allow_nan=False))
        self.payload = decode_state(encoded)

    async def close(self):
        return None

    def status(self):
        return "memory-test"


def test_tagged_json_round_trip_preserves_models_keys_sets_and_time():
    identifier = uuid4()
    observed = datetime.now(timezone.utc)
    value = {
        4: Position(latitude=1.35, longitude=103.85),
        "ids": {identifier},
        "observed": observed,
    }
    restored = decode_state(json.loads(json.dumps(encode_state(value))))
    assert restored[4] == value[4]
    assert restored["ids"] == {identifier}
    assert restored["observed"] == observed


def test_facilities_state_survives_store_recreation():
    async def scenario():
        repository = MemoryRepository()
        site = BY_ID["mock-cbd-01"]
        request = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-durable",
            expected_zone_id=site.zone_id, expected_position=site.position)

        original = FacilitiesStore(repository=repository)
        await original.start()
        receipt = await original.reserve(site.facility_id, request)
        await original.change(receipt["dispatch_id"], "OCCUPIED")

        restored = FacilitiesStore(repository=repository)
        await restored.start()
        assert restored.instance_id == original.instance_id
        assert await restored.get_hold(receipt["dispatch_id"]) == {
            **receipt, "status": "OCCUPIED"}
        assert (await restored.get(site.facility_id)).parking_available == site.parking_available - 1

    asyncio.run(scenario())


def test_dispatch_journal_and_idempotency_indexes_survive_recreation(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        approval = DispatchApproval(request_id=uuid4(), recommendation_id=uuid4())
        record = await service.store.begin(approval)
        record.data.update(vehicle_id="ev-durable", state="SUCCEEDED")
        await service.persist()

        restored = DispatchService(None, clock=clock, repository=repository)
        await restored.start()
        same = await restored.store.begin(approval)
        assert same.data["dispatch_id"] == record.data["dispatch_id"]
        assert same.data["vehicle_id"] == "ev-durable"
        assert same.data["state"] == "SUCCEEDED"

    asyncio.run(scenario())
