import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import uuid4
from fastapi import HTTPException


@dataclass
class DispatchRecord:
    data: dict
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    claim_request: dict | None = None
    parking_request: dict | None = None
    claimed: bool = False
    ack: dict | None = None
    events: dict = field(default_factory=dict)
    event_sequence: int = -1
    sent_tick: float | None = None
    claim_tick: float | None = None
    progress_tick: float | None = None
    release_outcome: str | None = None

    def view(self):
        return deepcopy(self.data)


class DispatchStore:
    def __init__(self, clock):
        self.clock = clock
        self.lock = asyncio.Lock()
        self.records = {}
        self.requests = {}
        self.recommendations = {}

    async def begin(self, body):
        async with self.lock:
            request_id, recommendation_id = str(body.request_id), str(body.recommendation_id)
            if request_id in self.requests:
                record = self.records[self.requests[request_id]]
                if record.data["recommendation_id"] != recommendation_id:
                    raise HTTPException(409, "REQUEST_ID_CONFLICT")
                return record
            if recommendation_id in self.recommendations:
                raise HTTPException(409, "RECOMMENDATION_ALREADY_APPROVED:" + self.recommendations[recommendation_id])
            if len(self.records) >= 10000:
                raise HTTPException(503, "DISPATCH_CAPACITY_REACHED")
            dispatch_id = str(uuid4())
            record = DispatchRecord({"dispatch_id": dispatch_id, "request_id": request_id,
                "recommendation_id": recommendation_id, "vehicle_id": None, "command": None,
                "state": "PENDING", "created_at": self.clock.now().isoformat(),
                "updated_at": self.clock.now().isoformat(), "acknowledged_at": None,
                "completed_at": None, "last_contact_at": None, "delivery_attempts": 0,
                "distance_travelled_km": 0, "arrival_soc_pct": None, "failure_reason": None,
                "claim_held": False, "parking_facility_id": None,
                "parking_status": None})
            self.records[dispatch_id] = record
            self.requests[request_id] = dispatch_id
            self.recommendations[recommendation_id] = dispatch_id
            return record

    def get(self, dispatch_id):
        record = self.records.get(str(dispatch_id))
        if not record:
            raise HTTPException(404, "DISPATCH_NOT_FOUND")
        return record
