import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type {
  BatchApprovalGroup, BatchApprovalGroupPage, BatchApprovalGroupPlan, BatchApprovalGroupRequest, RecommendationRequest,
} from '../api/contracts';
import { usePendingApprovals } from '../hooks/usePendingApprovals';
import { usePolling } from '../hooks/usePolling';
import { approvalDetails, batchApprovalLabels } from './approvalMessages';
import { BatchResult } from './BatchPanel';

const groupPath = '/api/dispatch/batch-approval-groups';
const groupPageSize = 15;
export const maxBulkPlans = 10;
type PendingGroup = { request_id: string; batch_recommendation_ids: string[] };

async function readUnresolvedGroups(path: string, options?: RequestInit): Promise<BatchApprovalGroupPage> {
  const first = await api<BatchApprovalGroupPage>(path, options);
  const groups = [...first.groups];
  let offset = first.next_offset;
  while (offset !== null) {
    const next = await api<BatchApprovalGroupPage>(`${path}&offset=${offset}`, options);
    groups.push(...next.groups);
    offset = next.next_offset;
  }
  return { ...first, groups, next_offset: null };
}

export const groupPlanLabels: Record<BatchApprovalGroupPlan['status'], string> = {
  ...batchApprovalLabels, QUEUED: 'Approval queued', RUNNING: 'Approving plan', SKIPPED: 'Approval skipped',
};

const groupLabels: Record<BatchApprovalGroup['status'], string> = {
  QUEUED: 'Queued', RUNNING: 'Approving plans', COMPLETED: 'Finished',
  CONFLICT: 'Vehicle conflict', UNKNOWN: 'Awaiting confirmation',
};

export function useBulkRecommendationApproval(enabledQueue: boolean, active: boolean) {
  const [offset, setOffset] = useState(0);
  const interval = active ? 2000 : 5000;
  const recent = usePolling<BatchApprovalGroupPage>(
    enabledQueue ? `${groupPath}?offset=0&limit=${groupPageSize}` : null, interval);
  const older = usePolling<BatchApprovalGroupPage>(
    enabledQueue && offset > 0 ? `${groupPath}?offset=${offset}&limit=${groupPageSize}` : null, interval);
  const history = offset > 0 ? older : recent;
  const unresolved = usePolling<BatchApprovalGroupPage>(
    enabledQueue ? `${groupPath}?unresolved=true&limit=100` : null, interval, readUnresolvedGroups);
  const [pending, updatePending] = usePendingApprovals<PendingGroup>('fleet.pending-bulk-approval', 'request_id');
  const [receipt, setReceipt] = useState<BatchApprovalGroup>();
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const [error, setError] = useState<string>();
  const saved = new Map<string, BatchApprovalGroup>();
  for (const group of [...(older.data?.groups ?? []), ...(unresolved.data?.groups ?? []), ...(recent.data?.groups ?? [])]) {
    const previous = saved.get(group.group_id);
    if (!previous || group.updated_at >= previous.updated_at) saved.set(group.group_id, group);
  }
  if (receipt && (saved.get(receipt.group_id)?.updated_at ?? '') < receipt.updated_at) {
    saved.set(receipt.group_id, receipt);
  }
  const groups = [...saved.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const waiting = pending.filter(item => !saved.has(item.request_id));
  const waitingIds = new Set(waiting.flatMap(item => item.batch_recommendation_ids));
  const plansByBatch = new Map<string, BatchApprovalGroupPlan>();
  for (const group of groups) {
    for (const plan of group.plans) {
      const previous = plansByBatch.get(plan.batch_recommendation_id);
      if (plan.status !== 'SKIPPED' && (!previous || (isActiveGroupPlan(plan) && !isActiveGroupPlan(previous)))) {
        plansByBatch.set(plan.batch_recommendation_id, plan);
      }
    }
  }
  const enabled = unresolved.data?.enabled === true;
  const stale = !!unresolved.error || !!recent.error || !!older.error;
  const ready = enabled && !unresolved.error;
  useEffect(() => {
    const confirmed = [...(older.data?.groups ?? []), ...(unresolved.data?.groups ?? []), ...(recent.data?.groups ?? [])];
    if (receipt && confirmed.some(group => group.group_id === receipt.group_id && group.updated_at >= receipt.updated_at)) {
      setReceipt(undefined);
    }
    for (const item of pending) {
      if (confirmed.some(group => group.group_id === item.request_id)) updatePending(item, false);
    }
  }, [recent.data, older.data, unresolved.data, pending, receipt, updatePending]);

  async function submit(groupId: string, ids: string[]) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setError(undefined);
    const pendingGroup = { request_id: groupId, batch_recommendation_ids: ids };
    const retryingUnconfirmed = waiting.some(item => item.request_id === groupId);
    updatePending(pendingGroup, true);
    const body: BatchApprovalGroupRequest = { group_id: groupId, batch_recommendation_ids: ids };
    try {
      const result = await api<BatchApprovalGroup>(groupPath, { method: 'POST', body: JSON.stringify(body) });
      setReceipt(result);
      updatePending(pendingGroup, false);
      setOffset(0);
    } catch (error) {
      const code = error instanceof ApiError ? error.message : '';
      if (['BATCH_APPROVAL_QUEUE_FULL', 'BATCH_APPROVAL_HISTORY_FULL', 'BATCH_APPROVAL_QUEUE_DISABLED',
        'BATCH_APPROVAL_QUEUE_UNAVAILABLE', 'BATCH_APPROVAL_GROUP_ID_CONFLICT', 'VALIDATION_ERROR'].includes(code)) {
        if (!retryingUnconfirmed) updatePending(pendingGroup, false);
        setError(code.includes('FULL') ? 'Dispatch approval is at capacity. Try again shortly.'
          : 'Dispatch did not accept this approval group. Check its status before trying again.');
      } else {
        setError('Dispatch did not confirm receipt. Retry the same group to check whether it was queued.');
      }
    } finally {
      sending.current = false;
      setBusy(false);
      recent.refresh();
      unresolved.refresh();
    }
  }

  const shownIds = new Set([
    ...(history.data?.groups ?? []), ...(unresolved.data?.groups ?? []), ...(receipt ? [receipt] : []),
  ].map(group => group.group_id));
  return { groups: groups.filter(group => shownIds.has(group.group_id)), waiting, waitingIds, plansByBatch,
    enabled, stale, ready, busy, error, submit, offset, setOffset,
    nextOffset: history.data?.next_offset, statusKnown: !enabledQueue || (!!unresolved.data && !unresolved.error) };
}

export function isActiveGroupPlan(plan?: BatchApprovalGroupPlan) {
  return plan?.status === 'QUEUED' || plan?.status === 'RUNNING' || plan?.status === 'UNKNOWN';
}

export function findVehicleConflicts(rows: RecommendationRequest[]) {
  const owners = new Map<string, string[]>();
  for (const row of rows) {
    for (const id of new Set(row.assignments.map(item => item.vehicle_id))) {
      owners.set(id, [...(owners.get(id) ?? []), row.request_id]);
    }
  }
  return [...owners.entries()].filter(([, requests]) => requests.length > 1);
}

export function BulkApprovalReview({ rows, unavailable, conflicts, now, canSubmit, zoneName, onRemove, onClose, onConfirm }: {
  rows: RecommendationRequest[];
  unavailable: Set<string>;
  conflicts: ReturnType<typeof findVehicleConflicts>;
  now: number;
  canSubmit: boolean;
  zoneName: (id: string) => string;
  onRemove: (id: string) => void;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog className="bulk-approval-review" ref={dialog} aria-labelledby="bulk-review-title" onCancel={onClose}>
    <h2 id="bulk-review-title">Approve selected plans</h2>
    <p>Dispatch accepts the selected plans in the order shown. The same EVs and destinations are authorized for up to 30 minutes; relocations start gradually after fresh safety and capacity checks. Some plans or EVs may be declined.</p>
    {rows.some(row => row.planning_mode === 'FULFIL_REQUEST') && <p className="warning" role="status">These plans prioritize the requested quantities. Coverage changes are advisory and may increase shortages in other zones; review the evidence before approving.</p>}
    <p className="muted">For charging assignments, staff confirm cable connection after arrival and disconnection after charging before the EV returns to service.</p>
    {conflicts.length > 0 && <p className="warning" role="status">Remove overlapping plans so each EV goes to one zone.</p>}
    {unavailable.size > 0 && <p className="warning" role="status">Remove expired or unavailable plans before confirming.</p>}
    {!canSubmit && !conflicts.length && !unavailable.size && rows.length > 0 &&
      <p className="warning" role="status">Wait for current Dispatch, fleet, demand and parking updates before confirming.</p>}
    <ol className="bulk-review-plans">
      {rows.map(row => {
        const seconds = Math.max(0, Math.ceil((Date.parse(row.expires_at ?? '') - now) / 1000));
        const conflictsForRow = conflicts.filter(([, ids]) => ids.includes(row.request_id)).map(([id]) => id);
        return <li key={row.request_id}>
          <div className="section-title"><strong>{zoneName(row.target_zone_id)}</strong>
            <span className={unavailable.has(row.request_id) ? 'warning' : 'muted'}>
              {unavailable.has(row.request_id) ? seconds ? 'Unavailable' : 'Expired' : `Expires in ${seconds}s`}
            </span>
          </div>
          <ul>{row.assignments.map(item => <li key={item.vehicle_id}>{item.vehicle_id} → {item.facility_name}</li>)}</ul>
          {conflictsForRow.length > 0 && <p className="warning">Shared EVs {conflictsForRow.join(', ')}</p>}
          <button type="button" onClick={() => onRemove(row.request_id)}>Remove {zoneName(row.target_zone_id)} plan</button>
        </li>;
      })}
    </ol>
    <div className="actions">
      <button autoFocus type="button" onClick={onClose}>Back to selection</button>
      <button type="button" className="primary" disabled={!canSubmit} onClick={onConfirm}>
        Confirm {rows.length} {rows.length === 1 ? 'plan' : 'plans'}
      </button>
    </div>
  </dialog>;
}

export function BulkApprovalProgress({ bulk, planName, onRelocation }: {
  bulk: ReturnType<typeof useBulkRecommendationApproval>;
  planName: (id: string) => string;
  onRelocation: (id: string) => void;
}) {
  if (!bulk.groups.length && !bulk.waiting.length && !bulk.error && !bulk.stale && !bulk.offset) return null;
  return <section className="request-queue-group" aria-labelledby="bulk-progress-title">
    <h3 id="bulk-progress-title">Bulk approvals</h3>
    {bulk.stale && <p className="warning" role="status">Bulk approval status could not be refreshed.</p>}
    {bulk.error && <p className="warning" role="status">{bulk.error}</p>}
    {bulk.waiting.map(item => <div className="request-queue-row bulk-progress-row" key={item.request_id}>
      <p>Receipt unconfirmed · {item.batch_recommendation_ids.map(planName).join(', ')}</p>
      <button disabled={bulk.busy || !bulk.ready} onClick={() => void bulk.submit(item.request_id, item.batch_recommendation_ids)}>
        Retry same group
      </button>
    </div>)}
    {bulk.groups.map(group => <details className="request-queue-row bulk-progress-row" key={group.group_id}>
      <summary><strong>{groupLabels[group.status]}</strong> · {group.plans.length} {group.plans.length === 1 ? 'plan' : 'plans'} · {new Date(group.created_at).toLocaleString()}</summary>
      {group.status === 'UNKNOWN' && <p>{approvalDetails('UNKNOWN', null, group.reason_code)}</p>}
      {group.conflicts.length > 0 && <ul>{group.conflicts.map(conflict => <li key={conflict.vehicle_id}>
        {conflict.vehicle_id} appears in {conflict.batch_recommendation_ids.map(planName).join(', ')}.
      </li>)}</ul>}
      <ol>{group.plans.map(plan => <li key={plan.batch_recommendation_id}>
        <strong>{planName(plan.batch_recommendation_id)}</strong> · {groupPlanLabels[plan.status]}
        {plan.reason_code && plan.status !== 'SKIPPED' && <p>{approvalDetails(
          plan.status === 'UNKNOWN' ? 'UNKNOWN' : 'REJECTED', null, plan.reason_code)}</p>}
        {plan.approval && <BatchResult result={plan.approval} onRelocation={onRelocation}/>}
      </li>)}</ol>
      {group.status === 'UNKNOWN' && <button disabled={bulk.busy || !bulk.ready} onClick={() => void bulk.submit(
        group.group_id, group.plans.map(plan => plan.batch_recommendation_id))}>Retry unresolved plans</button>}
    </details>)}
    {(bulk.offset > 0 || bulk.nextOffset != null) && <nav className="actions" aria-label="Bulk approval pages">
      <button disabled={!bulk.offset} onClick={() => bulk.setOffset(Math.max(0, bulk.offset - groupPageSize))}>Newer groups</button>
      <button disabled={bulk.nextOffset == null} onClick={() => {
        if (bulk.nextOffset != null) bulk.setOffset(bulk.nextOffset);
      }}>Older groups</button>
    </nav>}
  </section>;
}
