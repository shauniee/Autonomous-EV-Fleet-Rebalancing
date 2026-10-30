"""Operator-controlled synthetic disruptions, tied to APPROVED road geometry.

These are simulation events, never claims of real LTA incidents. Scenarios live
in the single Rebalancing replica's memory and deliberately expire/fail closed
on restart; no destructive changes to Fleet/Dispatch or their databases.
"""
import hmac
import os
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from ev_fleet.common.travel import distance_km

class TrafficScenarioInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dispatch_id: UUID
    segment_index: int = Field(ge=0, le=100000)
    kind: Literal["CONGESTION", "ROADWORKS", "ROAD_CLOSED"]
    speed_kph: float | None = Field(default=None, ge=3, le=80)
    duration_minutes: int = Field(default=30, ge=1, le=120)
    expected_version: int = Field(ge=0)

    def check(self):
        if self.kind == "ROAD_CLOSED" and self.speed_kph is not None:
            raise HTTPException(422, "Closure does not accept a speed")
        if self.kind != "ROAD_CLOSED" and self.speed_kph is None:
            raise HTTPException(422, "Congestion/roadworks require a speed")

def allowed_control(key):
    expected = os.getenv("TRAFFIC_SIMULATION_CONTROL_KEY", "")
    if (os.getenv("TRAFFIC_SIMULATION_ENABLED", "false").lower() != "true"
            or len(expected) < 16 or not hmac.compare_digest(key or "", expected)):
        raise HTTPException(403, "TRAFFIC_SIMULATION_CONTROL_DISABLED")

class TrafficScenarios:
    def __init__(self, clock):
        self.clock = clock
        self.version = 0
        self.entries = {}

    def _active(self):
        now = self.clock.now()
        expired = [key for key, row in self.entries.items()
                   if now.isoformat() >= row["expires_at"]]
        for key in expired:
            del self.entries[key]
            self.version += 1
        return list(self.entries.values())

    def list(self):
        return {"version": self.version, "enabled":
                os.getenv("TRAFFIC_SIMULATION_ENABLED", "false").lower() == "true",
                "scenarios": self._active(), "generated_at": self.clock.now().isoformat()}

    def create(self, body, route):
        body.check()
        self._active()
        if self.version != body.expected_version:
            raise HTTPException(409, "TRAFFIC_SCENARIO_VERSION_CONFLICT")
        if route is None or route.source not in ("ONEMAP", "OSRM"):
            raise HTTPException(409, "APPROVED_ROAD_GEOMETRY_REQUIRED")
        points = route.geometry
        if body.segment_index >= len(points) - 1:
            raise HTTPException(422, "SEGMENT_OUT_OF_RANGE")
        start, end = points[body.segment_index], points[body.segment_index + 1]
        distance = distance_km(start.model_dump(), end.model_dump())
        if distance < .001 or distance > 3:
            raise HTTPException(422, "INVALID_ROAD_SEGMENT")
        if any(not (1.1 <= p.latitude <= 1.5 and 103.5 <= p.longitude <= 104.1)
               for p in (start, end)):
            raise HTTPException(422, "SEGMENT_OUTSIDE_SINGAPORE")
        if len(self.entries) >= 30:
            raise HTTPException(409, "TRAFFIC_SCENARIO_CAPACITY_REACHED")
        now = self.clock.now()
        ident = str(uuid4())
        row = {
            "id": ident, "dispatch_id": str(body.dispatch_id), "segment_index": body.segment_index,
            "kind": body.kind, "speed_kph": body.speed_kph, "source": "SIMULATION",
            "start": start.model_dump(), "end": end.model_dump(),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=body.duration_minutes)).isoformat(),
        }
        self.entries[ident] = row
        self.version += 1
        return self.list()

    def delete(self, ident, expected_version):
        self._active()
        if self.version != expected_version:
            raise HTTPException(409, "TRAFFIC_SCENARIO_VERSION_CONFLICT")
        if ident not in self.entries:
            raise HTTPException(404, "TRAFFIC_SCENARIO_NOT_FOUND")
        del self.entries[ident]
        self.version += 1
        return self.list()

    def clear(self, expected_version):
        self._active()
        if self.version != expected_version:
            raise HTTPException(409, "TRAFFIC_SCENARIO_VERSION_CONFLICT")
        self.entries.clear()
        self.version += 1
        return self.list()

    def merged_snapshot(self, live):
        rows = self._active()
        if not rows:
            return {**live, "mode": "LIVE" if live["source"] == "LTA_DATAMALL" else "DISABLED",
                    "simulated_events": [], "scenario_version": self.version}
        # Explicit SIMULATION mode overrides live LTA rather than mixing
        # synthetic and observational evidence indistinguishably.
        speeds = []
        incidents = []
        works = []
        for row in rows:
            if row["kind"] in ("CONGESTION", "ROADWORKS"):
                speeds.append({"id": row["id"], "road": "SIMULATED ROUTE SEGMENT",
                               "start": row["start"], "end": row["end"],
                               "min_kph": row["speed_kph"], "max_kph": row["speed_kph"]})
                if row["kind"] == "ROADWORKS":
                    works.append({"id": row["id"], "road": "SIMULATED ROUTE SEGMENT",
                                  "start_date": row["created_at"][:10], "end_date": row["expires_at"][:10]})
            elif row["kind"] == "ROAD_CLOSED":
                # A route segment is unambiguously closed in simulation.
                incidents.append({"id": row["id"], "type": "ROAD BLOCK",
                    "position": row["start"], "message": "Synthetic closure on approved road route"})
        return {"source": "SIMULATION", "mode": "SIMULATION", "status": "FRESH",
                "generated_at": self.clock.now().isoformat(),
                "feeds": {}, "speeds": speeds, "incidents": incidents,
                "roadworks": works, "simulated_events": rows, "scenario_version": self.version}
