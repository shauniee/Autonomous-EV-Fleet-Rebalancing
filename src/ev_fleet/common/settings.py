import os
from pydantic import Field, model_validator
from ev_fleet.contracts.common import Model


class Settings(Model):
    stale_seconds: float = Field(default=15, gt=0)
    offline_seconds: float = Field(default=30, gt=0)
    min_reserve_pct: float = Field(default=20, ge=0, lt=100)
    route_factor: float = Field(default=1.3, ge=1)
    energy_margin_factor: float = Field(default=1.15, ge=1)
    distance_scale_km: float = Field(default=50, gt=0)
    recommendation_ttl_seconds: float = Field(default=10, gt=0)
    charging_max_age_seconds: float = Field(default=3600, gt=0)
    fleet_url: str = "http://localhost:8001"
    demand_url: str = "http://localhost:8002"

    @model_validator(mode="after")
    def thresholds(self):
        if self.offline_seconds < self.stale_seconds:
            raise ValueError("offline_seconds must be >= stale_seconds")
        return self

    @classmethod
    def from_env(cls):
        return cls(**{name: os.environ[name.upper()] for name in cls.model_fields
                      if name.upper() in os.environ})


class DispatchSettings(Settings):
    rebalancing_url: str = "http://localhost:8003"
    ack_timeout_seconds: float = Field(default=15, gt=0)
    progress_timeout_seconds: float = Field(default=30, gt=0)
    reconcile_interval_seconds: float = Field(default=1, gt=0)
    arrival_tolerance_km: float = Field(default=.05, gt=0, le=1)
