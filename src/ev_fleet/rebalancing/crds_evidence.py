"""Explain the guards and ranking CRDS already applied to an assessment."""
import asyncio
from collections.abc import Iterable
from typing import Literal
from uuid import UUID

import httpx
from pydantic import Field

from ev_fleet.contracts.common import Model
from ev_fleet.contracts.crds import CrdsEvaluation, CrdsMetadata
from ev_fleet.contracts.vehicles import VehicleState


class VehicleDecision(Model):
    outcome: Literal["RECOMMENDED", "EXCLUDED", "NOT_SELECTED", "INCOMPLETE"]
    reason: str
    facility_id: str | None = None
    related_reasons: list[str] = Field(default_factory=list)
    dispatch_ids: list[UUID] = Field(default_factory=list)
    arrival_soc_pct: float | None = None
    reserve_pct: float | None = None
    gain_vehicle_minutes: float | None = None
    origin_shortage_increase: float | None = None


class DecisionEvidence(Model):
    version: Literal[1] = 1
    assessment_reasons: list[str]
    batch_stop_reasons: list[str] = Field(default_factory=list)
    vehicles: dict[str, VehicleDecision]


def publish_evidence(metadata: CrdsMetadata, vehicles: dict[str, VehicleDecision], *,
                     reasons: Iterable[str] = (), batch_stop: Iterable[str] = ()):
    metadata.source_references["decision_evidence"] = DecisionEvidence(
        assessment_reasons=list(reasons) or metadata.reason_code.split(","),
        batch_stop_reasons=list(batch_stop),
        vehicles=dict(sorted(vehicles.items())),
    ).model_dump(mode="json", exclude_none=True)


def availability_reason(vehicle: VehicleState) -> str:
    if vehicle.telemetry is None:
        return "NO_TELEMETRY"
    if vehicle.connectivity != "ONLINE":
        return "OFFLINE"
    if vehicle.active_rental:
        return "RENTED"
    if vehicle.active_charging_task or vehicle.telemetry.charging_state == "CHARGING":
        return "CHARGING"
    if (vehicle.active_claim or vehicle.telemetry.active_dispatch_id
            or vehicle.telemetry.relocation_state != "IDLE"):
        return "BUSY"
    return "UNAVAILABLE"


def route_failure_reason(error: Exception) -> str:
    if isinstance(error, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "ROUTE_LOOKUP_TIMEOUT"
    if isinstance(error, httpx.HTTPStatusError):
        try:
            code = error.response.json()["error"]["code"]
        except (ValueError, KeyError, TypeError):
            return "ROUTE_EVIDENCE_UNAVAILABLE"
        if code == "ONEMAP_ROUTE_REQUIRED":
            return "ROUTING_NOT_CONFIGURED"
        if code in ("ONEMAP_AUTH_FAILED", "ONEMAP_TOKEN_MISSING"):
            return "ROUTING_ACCESS_UNAVAILABLE"
        if code in ("ONEMAP_NO_ROUTE", "ONEMAP_ORIGIN_NOT_ON_ROAD",
                    "ONEMAP_FACILITY_NO_ROAD_ACCESS", "ONEMAP_ENDPOINT_TOO_FAR"):
            return "ROAD_ROUTE_NOT_FOUND"
        if code == "ONEMAP_TIMEOUT":
            return "ROUTE_LOOKUP_TIMEOUT"
    if isinstance(error, ValueError) and str(error) in (
            "ROUTE_ORIGIN_CHANGED", "ROUTE_ESTIMATE_IDENTITY_CHANGED", "CHARGING_AVAILABILITY_UNKNOWN"):
        return str(error)
    return "ROUTE_EVIDENCE_UNAVAILABLE"


def ranked_decisions(result: CrdsEvaluation, minimum_gain: float,
                     origins: dict[str, str]) -> dict[str, VehicleDecision]:
    decisions = {}
    if not result.ranked_candidates or result.decision == "DEFER":
        return decisions
    winner = result.ranked_candidates[0]
    for gain in result.ranked_candidates:
        if gain.vehicle_id == result.selected_vehicle_id:
            outcome, reason = "RECOMMENDED", "USEFUL_SHORTAGE_REDUCTION"
        elif gain.worst_case_gain_vehicle_minutes <= minimum_gain:
            outcome, reason = "NOT_SELECTED", "NO_USEFUL_MOVE"
        elif gain.worst_case_gain_vehicle_minutes < winner.worst_case_gain_vehicle_minutes:
            outcome, reason = "NOT_SELECTED", "LOWER_MODELED_BENEFIT"
        elif gain.worst_case_energy_required_kwh > winner.worst_case_energy_required_kwh:
            outcome, reason = "NOT_SELECTED", "HIGHER_ENERGY_TIEBREAK"
        else:
            outcome, reason = "NOT_SELECTED", "STABLE_ORDER_TIEBREAK"
        origin = origins.get(gain.vehicle_id)
        increase = gain.shortage_changes_by_zone.get(origin, 0) if origin else 0
        decisions[gain.vehicle_id] = VehicleDecision(
            outcome=outcome, reason=reason, facility_id=gain.facility_id,
            gain_vehicle_minutes=gain.worst_case_gain_vehicle_minutes,
            origin_shortage_increase=increase if increase > 0 else None)
    return decisions
