import os
import re
from typing import Literal
from pydantic import Field, field_validator, model_validator
from ev_fleet.contracts.common import Model


class Settings(Model):
    stale_seconds: float = Field(default=15, gt=0)
    offline_seconds: float = Field(default=30, gt=0)
    min_reserve_pct: float = Field(default=20, ge=0, lt=100)
    route_factor: float = Field(default=1.3, ge=1)
    energy_margin_factor: float = Field(default=1.15, ge=1)
    distance_scale_km: float = Field(default=50, gt=0)
    recommendation_ttl_seconds: float = Field(default=120, gt=0)
    charging_max_age_seconds: float = Field(default=3600, gt=0)
    fleet_url: str = "http://localhost:8001"
    demand_url: str = "http://localhost:8002"
    facilities_url: str = "http://localhost:8005"

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


class SimulatorRecoverySettings(Model):
    enabled: bool = False
    vehicle_ids: list[str] = Field(default_factory=list, max_length=1000)
    lease_seconds: float = Field(default=30, gt=0, le=300)
    simulation_time_scale: float = Field(default=10, gt=0)
    dispatch_url: str = "http://localhost:8004"

    @field_validator("vehicle_ids", mode="before")
    @classmethod
    def parse_vehicle_ids(cls, value):
        values = [v.strip() for v in value.split(",") if v.strip()] if isinstance(value, str) else value
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v) for v in values):
            raise ValueError("simulator recovery requires explicit valid vehicle IDs")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def explicit_identities(self):
        if self.enabled and not self.vehicle_ids:
            raise ValueError("simulator recovery requires a server-configured vehicle allowlist")
        return self

    @classmethod
    def from_env(cls):
        values = {name: os.environ["SIMULATOR_RECOVERY_" + name.upper()]
                  for name in cls.model_fields if "SIMULATOR_RECOVERY_" + name.upper() in os.environ}
        values.setdefault("simulation_time_scale", os.getenv("SIMULATION_TIME_SCALE", "10"))
        values.setdefault("dispatch_url", os.getenv("DISPATCH_URL", "http://localhost:8004"))
        return cls(**values)


class CrdsSettings(Model):
    mode: Literal["DISABLED", "ADVISORY", "ACTIVE"] = "ACTIVE"
    horizon_minutes: int = Field(default=30, gt=0, le=120)
    slot_minutes: int = Field(default=1, gt=0)
    minimum_gain_vehicle_minutes: float = Field(default=1, ge=0)
    demand_step: int = Field(default=1, ge=0, le=20)
    return_delay_minutes: float = Field(default=10, ge=0, le=120)
    journey_delay_minutes: float = Field(default=5, ge=0, le=120)
    age_step_seconds: float = Field(default=30, gt=0)
    max_age_levels: int = Field(default=3, ge=0, le=10)
    expand_age: bool = True
    deadline_seconds: float = Field(default=30, gt=0, le=120)
    dispatch_url: str = "http://localhost:8004"
    execution_mode: Literal["LOCAL", "HTTP"] = "LOCAL"
    request_queue_capacity: int = Field(default=20, ge=1, le=100)
    request_queue_max_wait_seconds: float = Field(default=300, gt=0, le=3600)
    worker_urls: list[str] = Field(default_factory=list, max_length=8)
    worker_timeout_seconds: float = Field(default=5, gt=0, le=30)
    simulation_time_scale: float = Field(default=10, gt=0)

    @field_validator("worker_urls", mode="before")
    @classmethod
    def parse_worker_urls(cls, value):
        from urllib.parse import urlsplit

        values = [part.strip().rstrip("/") for part in value.split(",") if part.strip()] if isinstance(value, str) else value
        for url in values:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("worker URLs require server-configured HTTP(S) addresses without credentials")
        return values

    @model_validator(mode="after")
    def whole_slots(self):
        if self.horizon_minutes % self.slot_minutes:
            raise ValueError("CRDS horizon must contain whole slots")
        return self

    @classmethod
    def from_env(cls):
        values = {name: os.environ["CRDS_" + name.upper()] for name in cls.model_fields
                  if "CRDS_" + name.upper() in os.environ}
        values.setdefault("dispatch_url", os.getenv("DISPATCH_URL", "http://localhost:8004"))
        values.setdefault("simulation_time_scale", os.getenv("SIMULATION_TIME_SCALE", "10"))
        return cls(**values)
