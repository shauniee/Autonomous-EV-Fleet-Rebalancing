"""Simulated dispatcher-approved charging tasks; these do not operate real chargers."""
from typing import Literal
from uuid import UUID

from pydantic import Field

from .common import Model


class ChargingTaskCreate(Model):
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    request_id: UUID
    expected_version: int = Field(ge=1)


class ChargingTaskUpdate(Model):
    request_id: UUID
    expected_version: int = Field(ge=1)
    action: Literal["ASSIGN", "START_WORK", "COMPLETE", "CANCEL"]
    staff_name: str | None = Field(default=None, min_length=1, max_length=64)
