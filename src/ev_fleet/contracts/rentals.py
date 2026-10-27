"""Simulation-only rental lifecycle. Customer identities and payments are out of scope."""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .common import Model, Position


class RentalStart(Model):
    rental_id: UUID
    request_id: UUID
    expected_version: int = Field(ge=1)
    expected_end_at: AwareDatetime


class RentalReturn(Model):
    request_id: UUID
    expected_version: int = Field(ge=1)


class RentalRecord(Model):
    rental_id: UUID
    status: Literal["RENTED", "RETURNED"]
    expected_end_at: AwareDatetime
    started_at: AwareDatetime
    start_position: Position
    start_soc_pct: float = Field(ge=0, le=100)
    returned_at: AwareDatetime | None = None
    return_position: Position | None = None
    return_soc_pct: float | None = Field(default=None, ge=0, le=100)
    return_observed_at: AwareDatetime | None = None
