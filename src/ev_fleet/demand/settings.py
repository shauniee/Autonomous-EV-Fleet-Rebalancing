import os
from pydantic import Field, model_validator
from ev_fleet.contracts.common import Model


class DemandSettings(Model):
    simulation_enabled: bool = False
    random_seed: int = 42
    update_interval_seconds: float = Field(default=30, gt=0)
    min_demand: int = Field(default=0, ge=0, le=10000)
    max_demand: int = Field(default=20, ge=0, le=10000)
    max_step: int = Field(default=3, ge=1, le=10000)
    manual_hold_seconds: float = Field(default=60, ge=0)
    demand_max_age_seconds: float = Field(default=300, gt=0)
    fleet_snapshot_max_age_seconds: float = Field(default=5, gt=0)
    telemetry_max_age_seconds: float = Field(default=15, gt=0)
    zone_radius_km: float = Field(default=2, gt=0)
    min_reserve_pct: float = Field(default=20, ge=0, lt=100)
    critical_shortage_ratio: float = Field(default=.5, gt=0, le=1)
    medium_demand: int = Field(default=5, ge=1)
    high_demand: int = Field(default=10, ge=1)
    fleet_timeout_seconds: float = Field(default=2, gt=0)
    fleet_url: str = "http://localhost:8001"
    facilities_url: str | None = None

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.min_demand > self.max_demand:
            raise ValueError("min_demand must not exceed max_demand")
        if self.medium_demand >= self.high_demand:
            raise ValueError("medium_demand must be below high_demand")
        return self

    @classmethod
    def from_env(cls):
        names = {field: "DEMAND_" + field.upper() for field in cls.model_fields}
        names.update(fleet_url="FLEET_URL", facilities_url="FACILITIES_URL", min_reserve_pct="MIN_RESERVE_PCT",
                     demand_max_age_seconds="DEMAND_MAX_AGE_SECONDS")
        return cls(**{field: os.environ[name] for field, name in names.items() if name in os.environ})
