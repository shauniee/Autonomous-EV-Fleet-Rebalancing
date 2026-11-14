import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';
import type { BatchApproval, BatchApprovalLookup, RecommendationRequest, RecommendationRequestPage } from '../api/contracts';
import { usePolling, type Resource } from '../hooks/usePolling';
import { approvalErrorMessage, batchInProgress, batchProgressLabel } from './approvalMessages';
import { BatchConfirmation, BatchPanel, BatchResult } from './BatchPanel';
import {
  BulkApprovalProgress, BulkApprovalReview, findVehicleConflicts, groupPlanLabels, isActiveGroupPlan, maxBulkPlans,
  type useBulkRecommendationApproval,
} from './BulkRecommendationApproval';
import { crdsMessage } from './crdsMessages';

export const recommendationRequestsPath = '/api/rebalancing/recommendation-requests';
export const recommendationPageSize = 30;

const labels: Record<RecommendationRequest['status'], string> = {
  QUEUED: 'Queued', EVALUATING: 'Evaluating', READY: 'Ready for review',
  HOLD: 'No move recommended', DEFERRED: 'Deferred', ADVISORY: 'Advisory only',
  FAILED: 'Assessment failed', EXPIRED: 'Expired',
};

export interface QueuedBatchApproval extends BatchApproval { queued?: true }

export function requestApprovalStatus(row: RecommendationRequest, now: number,
  outcomes?: BatchApprovalLookup, pending: QueuedBatchApproval[] = []) {
  const outcome = outcomes?.batches.find(item => item.request_id === row.approval_request_id);
  const refusal = outcomes?.refusals.find(item => item.request_id === row.approval_request_id);
  const unresolved = pending.find(item => item.request_id === row.approval_request_id);
  const remaining = row.expires_at ? Math.max(0, Math.ceil((Date.parse(row.expires_at) - now) / 1000)) : 0;
  const eligible = row.status === 'READY' && remaining > 0 && !outcome && !refusal && !unresolved;
  const ready = eligible && !!outcomes;
  const running = (eligible && !outcomes) || row.status === 'QUEUED' || row.status === 'EVALUATING' ||
    (!refusal && (outcome ? (batchInProgress(outcome) || outcome.status === 'UNKNOWN') : !!unresolved));
  const status = refusal ? 'Approval rejected' : outcome ? batchProgressLabel(outcome)
    : unresolved ? 'Awaiting approval confirmation' : eligible && !outcomes ? 'Checking approval status' : row.status === 'READY' && !remaining
      ? labels.EXPIRED : labels[row.status];
  return { ready, running, status, outcome, refusal, unresolved, remaining };
}

interface Props {
  page: Resource<RecommendationRequestPage>;
  bulk: ReturnType<typeof useBulkRecommendationApproval>;
  rows: RecommendationRequest[];
  focusUnavailable: boolean;
  approvals: Resource<BatchApprovalLookup>;
  pending: QueuedBatchApproval[];
  now: number;
  active: boolean;
  focus?: { id: string; revision: number };
  offset: number;
  onPage: (offset: number) => void;
  zoneName: (id: string) => string;
  onMap: (id: string) => void;
  onRevise?: (request: RecommendationRequest) => void;
  onRelocation: (id: string) => void;
  onApprove: (approval: QueuedBatchApproval) => void;
  onCheck: (requestId: string, item?: string) => void;
  approvalBusy: boolean;
  feedsFresh: boolean;
}

export function RecommendationRequests(props: Props) {
  const { page, bulk, rows, focusUnavailable, approvals, pending, now, active, focus, offset, onPage, zoneName, onMap, onRevise,
    onRelocation, onApprove, onCheck, approvalBusy, feedsFresh } = props;
  const [expanded, setExpanded] = useState<string>();
  const [confirming, setConfirming] = useState<string>();
  const [selected, setSelected] = useState<RecommendationRequest[]>([]);
  const [bulkReview, setBulkReview] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const focusedRow = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!focus) return;
    setExpanded(focus.id);
    heading.current?.focus();
  }, [focus]);
  const detail = usePolling<RecommendationRequest>(
    active && expanded ? `${recommendationRequestsPath}/${expanded}` : null, 3000);
  const pageCount = page.data?.requests.length ?? 0;
  const states = rows.map(row => {
    const base = requestApprovalStatus(row, now, approvals.data, pending);
    const batchId = row.batch_recommendation_id ?? '';
    const groupPlan = bulk.plansByBatch.get(batchId);
    const waiting = bulk.waitingIds.has(batchId);
    const groupActive = groupPlan && isActiveGroupPlan(groupPlan);
    const individualOutcome = base.outcome;
    const outcome = individualOutcome && individualOutcome.status !== 'UNKNOWN'
      ? individualOutcome : groupPlan?.approval ?? individualOutcome;
    const status = waiting ? 'Bulk approval receipt unconfirmed' : groupActive ? groupPlanLabels[groupPlan.status]
      : outcome ? batchProgressLabel(outcome)
        : base.refusal || !groupPlan ? base.status : groupPlanLabels[groupPlan.status];
    return {
      ...base, row, outcome, groupPlan, status, groupManaged: waiting || !!groupActive,
      ready: base.ready && !groupPlan && !waiting,
      running: groupActive || waiting || (outcome ? (batchInProgress(outcome) || outcome.status === 'UNKNOWN') : base.running),
    };
  });
  const readyIds = new Set(states.filter(state => state.ready).map(state => state.row.request_id));
  const unavailable = new Set(selected.filter(row => !readyIds.has(row.request_id)).map(row => row.request_id));
  const conflicts = findVehicleConflicts(selected);
  const canSubmitBulk = bulk.ready && !bulk.busy && !approvalBusy && feedsFresh &&
    !page.error && !approvals.error && selected.length > 0 && !unavailable.size && !conflicts.length;
  const removePlan = (id: string) => setSelected(current => current.filter(row => row.request_id !== id));
  function changePage(next: number) {
    setSelected([]);
    setExpanded(undefined);
    onPage(next);
  }
  const groups = [
    { label: 'Ready for review', rows: states.filter(state => state.ready)
      .sort((a, b) => a.remaining - b.remaining) },
    { label: 'In progress', rows: states.filter(state => state.running) },
    { label: 'History', rows: states.filter(state => !state.ready && !state.running) },
  ];
  const groupOrder = groups.map(group => group.rows.map(state => state.row.request_id).join(',')).join('|');
  useLayoutEffect(() => {
    if (active && focusedRow.current && document.activeElement === document.body) {
      document.getElementById(`review-${focusedRow.current}`)?.focus({ preventScroll: true });
    }
  }, [active, groupOrder]);
  const current = states.find(state => state.row.request_id === expanded);
  const batch = detail.data?.batch;
  const approvalId = current?.row.approval_request_id;
  const canApprove = !!current?.ready && !!approvalId && !!batch &&
    detail.data?.status === 'READY' && !detail.error && !!approvals.data && !approvals.error &&
    !page.error && feedsFresh && !approvalBusy && bulk.statusKnown;
  return <section className="panel request-queue" aria-labelledby="recommendations-title">
    <div className="section-title">
      <h2 id="recommendations-title" ref={heading} tabIndex={-1}>Recommendations</h2>
      {page.data && <span className="muted">{pageCount ? `Requests ${offset + 1}–${offset + pageCount}` : 'No requests on this page'}</span>}
    </div>
    {page.error && <p className="warning" role="status">Recommendation status is unavailable. Showing the last available requests.</p>}
    {focusUnavailable && <p className="warning" role="status">The linked request is unavailable.</p>}
    {approvals.error && <p className="warning" role="status">Dispatch approval status is unavailable. Wait for an update before approving a plan.</p>}
    {!page.data && !page.error && <p role="status">Loading recommendation requests…</p>}
    {page.data && !page.data.enabled && <p className="muted">The recommendation queue is disabled.</p>}
    {page.data && page.data.enabled && !rows.length && !offset &&
      <p className="empty">Request a recommendation from a zone on the Dispatch map. Its status will appear here.</p>}
    <BulkApprovalProgress bulk={bulk} onRelocation={onRelocation} planName={id => {
      const row = rows.find(row => row.batch_recommendation_id === id);
      return row ? zoneName(row.target_zone_id) : `Plan ${id.slice(0, 8)}`;
    }}/>
    {bulk.enabled && (readyIds.size > 0 || selected.length > 0) && <div className="bulk-selection">
      <div>
        <strong>{selected.length} selected</strong>
        <span className="muted"> · Up to {maxBulkPlans} plans</span>
      </div>
      <div className="actions">
        {selected.length > 0 && <button onClick={() => setSelected([])}>Clear selection</button>}
        <button className="primary" disabled={!selected.length || bulk.busy} onClick={() => setBulkReview(true)}>Approve selected</button>
      </div>
      {conflicts.length > 0 && <p className="warning" role="status">Some selected plans share an EV. Review the selection to choose which plans to keep.</p>}
      {unavailable.size > 0 && <p className="warning" role="status">A selected plan has expired or is unavailable. Review the selection to remove it.</p>}
    </div>}
    {groups.filter(group => group.rows.length).map(group => <section key={group.label} className="request-queue-group">
      <h3>{group.label}</h3>
      {group.rows.map(state => {
        const { row, outcome, refusal, unresolved, status, remaining, ready, groupPlan, groupManaged } = state;
        const checked = selected.some(item => item.request_id === row.request_id);
        const shared = conflicts.filter(([, ids]) => ids.includes(row.request_id)).map(([id]) => id);
        const retry: QueuedBatchApproval | undefined = row.approval_request_id && row.batch_recommendation_id
          ? { request_id: row.approval_request_id, batch_recommendation_id: row.batch_recommendation_id, queued: true }
          : undefined;
        const requestedIds = row.vehicle_ids ?? (row.vehicle_id ? [row.vehicle_id] : []);
        const open = expanded === row.request_id;
        const preview = open && detail.data?.request_id === row.request_id ? detail.data.batch : null;
        return <article className="request-queue-row" key={row.request_id} onFocus={() => { focusedRow.current = row.request_id; }}>
          <div className="request-queue-summary">
            {bulk.enabled && (ready || checked) && <input type="checkbox" checked={checked}
              aria-label={`Select plan for ${zoneName(row.target_zone_id)}`}
              disabled={!checked && selected.length >= maxBulkPlans}
              onChange={event => event.target.checked ? setSelected(current => [...current, row]) : removePlan(row.request_id)}/>}
            <button id={`review-${row.request_id}`} className="text-button" aria-expanded={open} aria-controls={`request-${row.request_id}`}
              aria-label={`Review recommendation for ${zoneName(row.target_zone_id)} submitted ${new Date(row.submitted_at).toLocaleTimeString()}`}
              onClick={() => { setExpanded(open ? undefined : row.request_id); setConfirming(undefined); }}>
              <strong>{zoneName(row.target_zone_id)}</strong>
            </button>
            <span>{status}</span>
            <span className="muted">{row.recommended_count === null ? `${row.quantity} requested`
              : `${row.recommended_count} of ${row.quantity} EVs recommended`}</span>
            <time className="muted" dateTime={row.submitted_at}>{new Date(row.submitted_at).toLocaleString()}</time>
            {ready && <span className="muted">Expires in {remaining}s</span>}
            {shared.length > 0 && <span className="warning">Shared EVs {shared.join(', ')}</span>}
          </div>
          <div id={`request-${row.request_id}`} hidden={!open} className="request-queue-detail">
            {open && <>
              {requestedIds.length > 0 && <p>Requested EVs <strong>{requestedIds.join(', ')}</strong></p>}
              {detail.error && <p className="warning" role="status">The full assessment is unavailable. Showing saved request details.</p>}
              {row.batch_recommendation_id && !detail.data && !detail.error && <p role="status">Loading assessment…</p>}
              {preview ? <BatchPanel batch={preview} onRelocation={onRelocation}/> : <>
                {row.reason_code && <p>{crdsMessage(row.reason_code, 'The assessment did not produce an approvable plan.')}</p>}
                {row.assignments.length > 0 && <ul>{row.assignments.map(item =>
                  <li key={item.vehicle_id}>{item.vehicle_id} → {item.facility_name}</li>)}</ul>}
                {row.vehicle_reasons && Object.keys(row.vehicle_reasons).length > 0 && <ul>
                  {Object.entries(row.vehicle_reasons).map(([id, reason]) =>
                    <li key={id}><strong>{id}</strong> · {crdsMessage(reason, 'CRDS did not recommend this EV.')}</li>)}
                </ul>}
              </>}
              {outcome && <BatchResult result={outcome} onRelocation={onRelocation} busy={approvalBusy}
                onClose={groupManaged ? undefined : item => onCheck(outcome.request_id, item)}/>}
              {refusal && <p className="warning">{approvalErrorMessage(new ApiError(409, refusal.reason_code), 'REJECTED')}</p>}
              {!refusal && groupPlan?.reason_code && <p className="warning">{approvalErrorMessage(
                new ApiError(409, groupPlan.reason_code), groupPlan.status === 'UNKNOWN' ? 'UNKNOWN' : 'REJECTED')}</p>}
              {ready && !feedsFresh && <p className="muted">Wait for current fleet, demand and parking data before approving.</p>}
              <div className="actions">
                <button onClick={() => onMap(row.target_zone_id)}>View on map</button>
                {onRevise && <button onClick={() => onRevise(row)}>
                  {row.status === 'EXPIRED' || (!remaining && row.status === 'READY') || refusal || outcome?.status === 'REJECTED' ||
                    groupPlan?.status === 'REJECTED' ? 'Request again' : 'Revise request'}
                </button>}
                {ready && <button className="primary" disabled={!canApprove} onClick={() => setConfirming(row.request_id)}>Review approval</button>}
                {!groupManaged && (!outcome?.job_status && (outcome?.status === 'UNKNOWN' || !!unresolved)) && !refusal && retry && <>
                  <button disabled={approvalBusy} onClick={() => onCheck(retry.request_id)}>Check approval status</button>
                  <button disabled={approvalBusy} onClick={() => onApprove(retry)}>Retry same approval</button>
                </>}
              </div>
            </>}
          </div>
        </article>;
      })}
    </section>)}
    {(offset > 0 || page.data?.next_offset != null) && <nav className="actions" aria-label="Recommendation pages">
      <button disabled={!offset} onClick={() => changePage(Math.max(0, offset - recommendationPageSize))}>Previous</button>
      <button disabled={page.data?.next_offset == null} onClick={() => {
        if (page.data?.next_offset != null) changePage(page.data.next_offset);
      }}>Next</button>
    </nav>}
    {active && bulkReview && <BulkApprovalReview rows={selected} unavailable={unavailable} conflicts={conflicts}
      now={now} canSubmit={canSubmitBulk} zoneName={zoneName} onRemove={removePlan} onClose={() => setBulkReview(false)}
      onConfirm={() => {
        if (!canSubmitBulk || selected.some(row => !row.batch_recommendation_id ||
            Date.now() >= Date.parse(row.expires_at ?? ''))) return;
        void bulk.submit(crypto.randomUUID(), selected.map(row => row.batch_recommendation_id!));
        setSelected([]);
        setBulkReview(false);
      }}/>}
    {active && confirming === expanded && batch && approvalId &&
      <BatchConfirmation batch={batch} expired={!canApprove} onClose={() => setConfirming(undefined)} onApprove={() => {
        if (!canApprove) return;
        setConfirming(undefined);
        onApprove({ request_id: approvalId, batch_recommendation_id: batch.batch_recommendation_id, queued: true });
      }}/>}
  </section>;
}
