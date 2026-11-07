import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { Dispatch, RelocationEndRequest, RelocationResolution } from '../api/contracts';
import { usePolling } from '../hooks/usePolling';
import type { Notify } from './Notifications';

const statusLabels = {
  REQUESTED: 'Waiting to confirm the stop',
  CLEANUP_PENDING: 'The vehicle stopped. Its destination space is still reserved',
  ENDED: 'Relocation ended',
  BLOCKED: 'Could not confirm the stop',
};

export function RelocationResolutionControl({ dispatchId, notify, refreshRecovery }: {
  dispatchId: string; notify: Notify; refreshRecovery: () => void;
}) {
  const base = `/api/dispatch/dispatches/${encodeURIComponent(dispatchId)}`;
  const key = `fleet.pending-relocation-stop.${dispatchId}`;
  const assessment = usePolling<RelocationResolution>(`${base}/resolution`, 5000);
  const [pending, setPending] = useState<RelocationEndRequest | null>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
      return typeof saved?.request_id === 'string' && typeof saved.expected_assessment_revision === 'string'
        && typeof saved.reason === 'string' ? saved : null;
    } catch { return null; }
  });
  const [reviewRevision, setReviewRevision] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const running = useRef(false);
  const operation = assessment.data?.operation;
  const currentRevision = assessment.data?.assessment_revision;
  const fresh = !!assessment.updatedAt && !assessment.error && Date.now() - Date.parse(assessment.updatedAt) < 15000;
  const canEnd = fresh && !!currentRevision && assessment.data?.can_end && !busy && !pending;

  useEffect(() => {
    if (pending && operation?.request_id === pending.request_id) {
      setPending(null);
      try { sessionStorage.removeItem(key); } catch { /* Dispatch owns the persisted outcome. */ }
    }
  }, [pending, operation?.request_id, key]);
  useEffect(() => { if (reviewRevision) dialog.current?.showModal(); }, [reviewRevision]);

  async function end(request: RelocationEndRequest) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setReviewRevision(null);
    setPending(request);
    try { sessionStorage.setItem(key, JSON.stringify(request)); } catch { /* The exact request stays in memory. */ }
    try {
      await api(`${base}/resolution`, { method: 'POST', body: JSON.stringify(request) });
    } catch (error) {
      if (error instanceof ApiError && [400, 404, 409, 422].includes(error.status)) {
        setPending(null);
        try { sessionStorage.removeItem(key); } catch { /* The assessment still reports backend status. */ }
        notify('warning', 'The stop was declined. Check the relocation status before trying again.');
      } else {
        notify('warning', 'Stop request unconfirmed. Check status or retry the request.');
      }
    } finally {
      running.current = false;
      setBusy(false);
      assessment.refresh();
      refreshRecovery();
    }
  }

  async function reconcile() {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    try {
      const result = await api<Dispatch>(`${base}/reconcile`, { method: 'POST' });
      notify('info', result.claim_held === false
        ? 'The vehicle is no longer assigned to this relocation.'
        : 'The vehicle may still be assigned to this relocation.');
    } catch {
      notify('warning', 'Could not check the relocation status. Try again.');
    } finally {
      running.current = false;
      setBusy(false);
      assessment.refresh();
      refreshRecovery();
    }
  }

  const unstarted = assessment.data?.unstarted;
  const actionLabel = unstarted ? 'Close unstarted relocation' : 'End relocation';
  const blocker = assessment.data?.blocker;
  const stopIssue = assessment.error ? 'Could not check whether the relocation can end. Select Check relocation status to try again.'
    : blocker && !operation ? ({
      'A simulator resume is already in progress.': 'Wait for the resume request to finish.',
      'Arrival is already recorded. Reconcile to confirm release.': 'Arrival was recorded. Check the relocation status.',
      'The simulator still owns a live lease and may be moving.': 'The vehicle may still be moving.',
    }[blocker] ?? blocker) : null;

  return <div className="relocation-controls" aria-label="End relocation controls">
    {operation && <p role="status"><strong>{statusLabels[operation.status]}.</strong></p>}
    {stopIssue && <p className="warning">{stopIssue}</p>}
    {pending && <p role="status">Waiting for confirmation of the stop request.</p>}
    <div className="actions">
      <button disabled={busy} onClick={() => void reconcile()}>Check relocation status</button>
      <button disabled={!canEnd} onClick={() => { setReason(''); setReviewRevision(currentRevision ?? null); }}>{actionLabel}</button>
      {pending && <button disabled={busy} onClick={() => void end(pending)}>Retry stop request</button>}
    </div>
    {reviewRevision && <dialog ref={dialog} onCancel={() => setReviewRevision(null)} aria-labelledby={`stop-title-${dispatchId}`}>
      <h2 id={`stop-title-${dispatchId}`}>{actionLabel}</h2>
      <p>{unstarted
        ? 'Closing this relocation releases the vehicle claim and destination reservation. The vehicle may stay unavailable until fresh telemetry and parking checks pass.'
        : 'Ending the relocation saves the vehicle’s last known position and battery level. The vehicle may stay unavailable until it can be parked safely.'}</p>
      {reviewRevision === currentRevision && assessment.data?.stop_position && <p>
        Recorded stop position {assessment.data.stop_position.latitude.toFixed(5)}, {assessment.data.stop_position.longitude.toFixed(5)}
        {assessment.data.stop_soc_pct != null && <> · Battery {assessment.data.stop_soc_pct.toFixed(1)}%</>}
      </p>}
      <label>Reason for ending the relocation <input value={reason} onChange={event => setReason(event.target.value)} maxLength={200} required /></label>
      {(!canEnd || reviewRevision !== currentRevision) && <p role="status">The relocation status changed or is unavailable. Close and review again.</p>}
      <div className="actions">
        <button autoFocus onClick={() => setReviewRevision(null)}>Leave open</button>
        <button className="primary" disabled={!canEnd || reviewRevision !== currentRevision || !reason.trim()}
          onClick={() => void end({ request_id: crypto.randomUUID(), expected_assessment_revision: reviewRevision, reason: reason.trim() })}>{unstarted ? 'Confirm closure' : 'Confirm end relocation'}</button>
      </div>
    </dialog>}
  </div>;
}
