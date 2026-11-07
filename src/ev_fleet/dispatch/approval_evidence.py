"""Record the evidence behind admission independently of journey status."""
from fastapi import HTTPException
from pydantic import ValidationError

from ev_fleet.contracts.crds import CrdsMetadata
from ev_fleet.contracts.dispatch import ApprovalDecision, ApprovalStatus
from ev_fleet.rebalancing.crds_evidence import DecisionEvidence
from .store import DispatchRecord


class ApprovalCheckError(HTTPException):
    """An existing approval error with facts captured at the check that failed."""

    def __init__(self, status_code: int, detail: str, decision: ApprovalDecision):
        super().__init__(status_code, detail)
        self.decision = decision


def approval_evidence(record: DispatchRecord) -> dict:
    return (record.data.get("revalidated_metrics") or {}).get("approval_decision") or {}


def record_approval(record: DispatchRecord, status: ApprovalStatus, reason: str,
                    decision: ApprovalDecision | None = None) -> ApprovalDecision:
    previous = approval_evidence(record)
    evidence = decision or ApprovalDecision(status=status, reason_code=reason)
    evidence = evidence.model_copy(update={
        "status": status,
        "vehicle_id": evidence.vehicle_id or record.data["vehicle_id"] or previous.get("vehicle_id"),
        "facility_id": evidence.facility_id or record.data["parking_facility_id"] or previous.get("facility_id"),
    })
    metrics = record.data.get("revalidated_metrics") or {}
    record.data["revalidated_metrics"] = {
        **metrics, "approval_decision": evidence.model_dump(mode="json", exclude_none=True),
    }
    return evidence


def revalidation_rejection(metadata: CrdsMetadata, vehicle_id: str,
                           facility_id: str | None) -> ApprovalDecision:
    reasons = []
    row = None
    try:
        evidence = DecisionEvidence.model_validate(metadata.source_references.get("decision_evidence"))
    except ValidationError:
        pass  # Older Rebalancing responses still carry the original reason code.
    else:
        reasons = evidence.assessment_reasons
        row = evidence.vehicles.get(vehicle_id)
    if metadata.decision == "DEFER":
        reason = reasons[0] if reasons else metadata.reason_code
        related = reasons[1:]
    elif row and row.outcome in ("EXCLUDED", "NOT_SELECTED"):
        reason, related = row.reason, row.related_reasons
    else:
        reason = metadata.reason_code if metadata.decision == "HOLD" else "CRDS_APPROVAL_RECHECK_FAILED"
        related = []
    return ApprovalDecision(
        status="REJECTED", reason_code=reason, related_reasons=related,
        vehicle_id=vehicle_id, facility_id=facility_id,
        arrival_soc_pct=row.arrival_soc_pct if row else None,
        reserve_pct=row.reserve_pct if row else None,
        gain_vehicle_minutes=row.gain_vehicle_minutes if row else None,
        minimum_gain_vehicle_minutes=metadata.parameters.minimum_gain_vehicle_minutes,
    )
