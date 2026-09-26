from datetime import datetime, timezone
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Position(Model):
    # Simulation envelope around Singapore, not a national-boundary validator.
    latitude: float = Field(ge=1.15, le=1.50)
    longitude: float = Field(ge=103.60, le=104.10)


class Observation(Model):
    observed_at: AwareDatetime
