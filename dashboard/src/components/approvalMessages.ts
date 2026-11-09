import { ApiError } from '../api/client';
import type { ApprovalDecision, ApprovalStatus, BatchDispatch, DispatchApprovalResult } from '../api/contracts';
import { crdsMessage } from './crdsMessages';
import { number } from './format';

export const approvalLabels: Record<ApprovalStatus, string> = {
  APPROVED: 'Approved', REJECTED: 'Rejected', UNKNOWN: 'Unconfirmed',
};

export const batchApprovalLabels: Record<BatchDispatch['status'], string> = {
  APPROVED: 'Approved', PARTIAL: 'Partially approved', REJECTED: 'Rejected', UNKNOWN: 'Approvals unconfirmed',
};

const approvalReasons: Readonly<Record<string, string>> = {
  APPROVAL_CONFIRMED: 'Dispatch confirmed the vehicle assignment.',
  CRDS_APPROVAL_CONFIRMED: 'Dispatch confirmed the vehicle assignment after checking the route, battery reserve, and benefit to the fleet.',
  APPROVAL_EXPIRED: 'The recommendation expired before the EV could be assigned.',
  RECOMMENDATION_EXPIRED: 'The recommendation expired before approval.',
  RECOMMENDATION_MISSING_OR_EXPIRED: 'The recommendation is no longer available. It may have expired.',
  BATCH_EXPIRED: 'The batch recommendation expired before approval.',
  CRDS_APPROVAL_DISABLED: 'CRDS approval is unavailable for this recommendation.',
  CRDS_ADVISORY_ONLY: 'This recommendation is available for review in advisory mode.',
  CRDS_POLICY_CHANGED: 'CRDS settings changed after the recommendation was made.',
  CRDS_PLANNING_CHANGED: 'Fleet plans changed while Dispatch was checking the recommendation.',
  CRDS_APPROVAL_RECHECK_FAILED: 'The fresh CRDS assessment could not confirm the recommended move.',
  NO_USEFUL_MOVE: 'The move no longer provides enough modeled benefit to the fleet.',
  DONOR_SERVICE_FLOOR: 'The origin zone needs its last available EV to serve demand.',
  NO_ELIGIBLE_RELOCATIONS: 'The fresh assessment found no eligible move for this recommendation.',
  NO_RECOMMENDED_VEHICLE: 'This recommendation has no EV to approve.',
  NO_BATCH_ASSIGNMENTS: 'This batch has no EV assignments to approve.',
  TARGET_CHANGED: 'The destination zone changed after the recommendation was made.',
  FLEET_INSTANCE_CHANGED: 'Fleet restarted while Dispatch was checking the EV.',
  VEHICLE_NOT_FOUND: 'The recommended EV is no longer in the fleet snapshot.',
  VEHICLE_VERSION_CONFLICT: 'The vehicle changed while Dispatch was checking its availability.',
  VEHICLE_NOT_ELIGIBLE: 'The EV is no longer eligible for this relocation.',
  VEHICLE_BUSY: 'The EV already has another assignment.',
  APPROVAL_RESOURCE_UNRESOLVED: 'Another approval for this EV or destination still needs confirmation.',
  PARKING_DESTINATION_CHANGED: 'The recommended parking destination changed or is no longer available.',
  PARKING_FACILITY_CHANGED: 'The recommended parking destination changed or is no longer available.',
  PARKING_FULL: 'The parking space is no longer available.',
  FACILITY_NOT_FOUND: 'The recommended parking destination is no longer available.',
  PARKING_ROAD_ACCESS_TOO_FAR: 'The checked route does not reach a usable entrance to the destination.',
  ONEMAP_ROUTE_REQUIRED: 'Dispatch could not verify a usable road route.',
  ONEMAP_AUTH_FAILED: 'Dispatch could not access the routing service. Contact support if this continues.',
  ONEMAP_TOKEN_MISSING: 'Dispatch could not access the routing service. Contact support if this continues.',
  ONEMAP_RATE_LIMITED: 'The routing service is busy. Try again shortly.',
  ONEMAP_TIMEOUT: 'The routing service did not respond in time.',
  ONEMAP_UNAVAILABLE: 'The routing service is temporarily unavailable.',
  ONEMAP_INVALID_RESPONSE: 'Dispatch could not verify the route returned by the routing service.',
  ONEMAP_NO_ROUTE: 'The routing service could not find a usable road route.',
  ONEMAP_ORIGIN_NOT_ON_ROAD: 'The EV’s location could not be matched to a usable road.',
  ONEMAP_FACILITY_NO_ROAD_ACCESS: 'The routing service could not find road access to the destination.',
  ONEMAP_ENDPOINT_TOO_FAR: 'The route ends too far from the destination.',
  TRAFFIC_ROUTE_BLOCKED_NO_SAFE_ALTERNATIVE: 'Traffic restrictions block the route, and Dispatch found no safe alternative.',
  CHARGING_TARGET_ALREADY_REACHED: 'The EV has already reached its charging target.',
  CHARGING_NOT_REQUIRED: 'The EV no longer needs this charging assignment.',
  CLAIM_ID_CONFLICT: 'Dispatch could not confirm which approval owns the vehicle assignment.',
  PARKING_HOLD_ID_CONFLICT: 'Dispatch could not confirm which approval owns the parking reservation.',
  REQUEST_ID_CONFLICT: 'This approval request is already linked to a different recommendation.',
  BATCH_REQUEST_ID_CONFLICT: 'This approval request is already linked to a different batch.',
  RECOMMENDATION_ALREADY_APPROVED: 'An approval already exists for this recommendation. Check Relocations for its status.',
  BATCH_ALREADY_APPROVED: 'An approval already exists for this batch. Check the original approval for its status.',
  APPROVAL_NOT_FOUND: 'Dispatch could not find the approval. Check or retry the original request.',
  BATCH_NOT_FOUND: 'Dispatch could not find the batch approval. Check or retry the original request.',
  APPROVAL_MAY_HAVE_ACQUIRED_RESOURCES: 'The approval may already hold the EV or a parking space. Check its status before closing it.',
  OPERATOR_CLOSED_UNSTARTED_APPROVAL: 'The operator closed this approval before relocation started.',
  PREPARATION_INCOMPLETE_RETRY_SAME_REQUEST: 'Dispatch paused before completing the vehicle assignment.',
  ADMISSION_BUSY_RETRY_SAME_REQUEST: 'Dispatch is busy checking approvals.',
  PERSISTENCE_STATE_UNCERTAIN: 'Dispatch could not confirm that the outcome was saved.',
};

const journeyLabels: Readonly<Record<string, string>> = {
  PENDING: 'Awaiting delivery', SENT: 'Sent to EV', ACKNOWLEDGED: 'Accepted by EV',
  IN_PROGRESS: 'In progress', ARRIVAL_PENDING: 'Confirming arrival', SUCCEEDED: 'Arrived',
  REJECTED: 'Declined by EV', FAILED: 'Failed', UNKNOWN: 'Unconfirmed',
};

export function journeyLabel(state: string) {
  return Object.prototype.hasOwnProperty.call(journeyLabels, state) ? journeyLabels[state] : 'Unavailable';
}

export function journeyIssue(state: string, reason: string | null) {
  switch (reason) {
    case 'OPERATOR_ENDED_RELOCATION': return 'Ended by an operator.';
    case 'VEHICLE_OFFLINE_OR_STALE': return 'Vehicle updates are unavailable. Check the relocation status.';
    case 'ACK_TIMEOUT': return 'The vehicle has not confirmed the move. Check the relocation status.';
    case 'BATTERY_DEPLETED': return 'The EV ran out of battery during relocation.';
    case 'PROGRESS_TIMEOUT': return 'Vehicle updates stopped. Check the relocation status.';
  }
  if (state === 'UNKNOWN') return 'The relocation status is unclear. Check for an update.';
  if (state === 'FAILED') return 'The relocation could not be completed.';
  if (state === 'REJECTED') return 'The EV declined the relocation after Dispatch approved it.';
  if (reason && state === 'ARRIVAL_PENDING') return 'Arrival has not been confirmed yet.';
  return '';
}

export function approvalStatus(dispatch: DispatchApprovalResult): ApprovalStatus {
  if (dispatch.approval_status) return dispatch.approval_status;
  const recorded = dispatch.revalidated_metrics?.approval_decision?.status;
  if (recorded === 'APPROVED' || recorded === 'REJECTED' || recorded === 'UNKNOWN') return recorded;
  if (dispatch.claim_held === true || dispatch.acknowledged_at || dispatch.state === 'SUCCEEDED'
    || dispatch.failure_reason === 'EV_REJECTED_COMMAND') return 'APPROVED';
  return dispatch.state === 'REJECTED' ? 'REJECTED' : 'UNKNOWN';
}

function reasonCodes(code: string) {
  const [base, detail] = code.split(':', 2);
  if ((base === 'CRDS_BENEFIT_CHANGED' || base === 'VEHICLE_NOT_ELIGIBLE') && detail) return detail.split(',');
  return base.split(',');
}

function reasonMessage(code: string) {
  const key = code.trim();
  return Object.prototype.hasOwnProperty.call(approvalReasons, key) ? approvalReasons[key] : crdsMessage(key, '');
}

function evidenceMessage(decision: unknown, fallback: string | null) {
  const evidence = decision as Partial<ApprovalDecision> | null;
  const code = typeof evidence?.reason_code === 'string' ? evidence.reason_code : fallback ?? '';
  const related = Array.isArray(evidence?.related_reasons) ? evidence.related_reasons : [];
  const reasons = [...reasonCodes(code), ...related.filter(reason => typeof reason === 'string')];
  const messages = [...new Set(reasons.map(reasonMessage).filter(Boolean))];
  if (!messages.length) return '';
  if (typeof evidence?.arrival_soc_pct === 'number' && Number.isFinite(evidence.arrival_soc_pct)
    && typeof evidence.reserve_pct === 'number' && Number.isFinite(evidence.reserve_pct)) {
    messages.push(evidence.arrival_soc_pct < 0
      ? `The EV is expected to run out of battery before arrival. Required reserve ${number(evidence.reserve_pct, '%')}.`
      : `Estimated battery on arrival ${number(evidence.arrival_soc_pct, '%')}. Required reserve ${number(evidence.reserve_pct, '%')}.`);
  }
  if (typeof evidence?.gain_vehicle_minutes === 'number' && Number.isFinite(evidence.gain_vehicle_minutes)
    && typeof evidence.minimum_gain_vehicle_minutes === 'number' && Number.isFinite(evidence.minimum_gain_vehicle_minutes)) {
    messages.push(`Minimum modeled benefit ${number(evidence.gain_vehicle_minutes, ' points')}. The move must exceed ${number(evidence.minimum_gain_vehicle_minutes, ' points')}.`);
  }
  return messages.join(' ');
}

export function approvalDetails(status: ApprovalStatus, decision: unknown, code: string | null = null) {
  const detail = evidenceMessage(decision, code);
  if (status === 'APPROVED') return detail || approvalReasons.APPROVAL_CONFIRMED;
  if (status === 'UNKNOWN') return detail || 'Dispatch could not confirm the approval outcome.';
  if (code === 'OPERATOR_CLOSED_UNSTARTED_APPROVAL' || code?.startsWith('RECOMMENDATION_ALREADY_APPROVED:')
    || code === 'BATCH_ALREADY_APPROVED' || code === 'CRDS_ADVISORY_ONLY') return detail;
  return `${detail || 'Dispatch could not approve the relocation.'} Request a fresh recommendation before trying again.`;
}

export function approvalErrorMessage(error: unknown, status: 'REJECTED' | 'UNKNOWN', checking = false) {
  const code = error instanceof ApiError ? error.message : null;
  const evidence = error instanceof ApiError ? error.approvalDecision : undefined;
  const reference = error instanceof ApiError && /^[0-9a-f-]{36}$/i.test(error.reference ?? '') ? error.reference : undefined;
  const message = checking
    ? evidenceMessage(evidence, code) || 'Dispatch could not check the approval status. Try checking again.'
    : approvalDetails(status, evidence, code);
  const outcome = checking ? message : status === 'UNKNOWN'
    ? `Approval is still unconfirmed. ${message} Check status or retry the same approval.` : message;
  return reference ? `${outcome} Support reference ${reference}.` : outcome;
}

export function dispatchApprovalMessage(dispatch: DispatchApprovalResult) {
  const status = approvalStatus(dispatch);
  const evidence = dispatch.revalidated_metrics?.approval_decision;
  const vehicle = dispatch.vehicle_id ?? evidence?.vehicle_id;
  const heading = status === 'UNKNOWN' ? 'Approval is still unconfirmed'
    : vehicle ? `${vehicle} · ${approvalLabels[status]}` : `Approval ${approvalLabels[status].toLowerCase()}`;
  const detail = approvalDetails(status, evidence, status === 'APPROVED' ? null : dispatch.failure_reason);
  const followup = status === 'UNKNOWN' ? 'Check status or retry the same approval.'
    : status === 'APPROVED' ? journeyIssue(dispatch.state, dispatch.failure_reason) || 'Follow the journey in Relocations.' : '';
  return [`${heading}.`, detail, followup].filter(Boolean).join(' ');
}
