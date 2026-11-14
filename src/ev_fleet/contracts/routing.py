from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .common import Model, Position


class RouteInstruction(Model):
    text: str
    distance_m: float = Field(ge=0)


# Sources Dispatch may approve. OFFLINE_DEMO (straight lines) only exists when
# Dispatch runs with ROUTING_PROVIDER=OFFLINE_DEMO, and it is labelled as such.
APPROVED_ROUTE_SOURCES = ("ONEMAP", "OFFLINE_DEMO")


class RoadRoute(Model):
    source: Literal["ONEMAP", "OSRM", "OFFLINE_DEMO"] = "ONEMAP"
    fetched_at: AwareDatetime
    geometry: list[Position] = Field(min_length=2, max_length=50000)
    distance_km: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    instructions: list[RouteInstruction] = Field(default_factory=list, max_length=5000)
    rerouted: bool = False
    traffic_assessment: dict | None = None

    @model_validator(mode="after")
    def duration_matches_distance(self):
        if self.distance_km > 0 and self.duration_seconds <= 0:
            raise ValueError("a nonzero route requires a positive duration")
        return self
