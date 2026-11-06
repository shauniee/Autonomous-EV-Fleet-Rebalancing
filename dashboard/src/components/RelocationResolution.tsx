import { useEffect, useRef, useState } from 'react';
import { api, ApiError, message } from '../api/client';
import type { Dispatch, RelocationEndRequest, RelocationResolution } from '../api/contracts';
import { usePolling } from '../hooks/usePolling';
import type { Notify } from './Notifications';

const statusLabels = {
  REQUESTED: 'Waiting for Fleet to confirm the stop',
  CLEANUP_PENDING: 'Vehicle claim released. Destination hold cleanup is pending.',
  ENDED: 'Relocation ended. Cleanup complete',
  BLOCKED: 'Fleet could not confirm the stop',
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
        notify('warning', 'The stop was not accepted. Review the current relocation evidence and try again.');
      } else {
        notify('warning', 'Stop confirmation is unavailable. Check status or retry the same request.');
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
        ? 'Dispatch reports no active claim for this relocation. Review the destination hold below.'
        : 'Dispatch has not confirmed claim release. Review the relocation evidence.');
    } catch (error) {
      notify('warning', `Relocation status could not be reconciled. ${message(error)}`);
    } finally {
      running.current = false;
      setBusy(false);
      assessment.refresh();
      refreshRecovery();
    }
  }

  return <div aria-label="Relocation resolution">
    {operation && <p role="status"><strong>{statusLabels[operation.status]}.</strong> {operation.reason}
      {operation.detail && <small className="identifier">{operation.detail}</small>}</p>}
    {assessment.error ? <p className="warning">Current stop eligibility is unavailable.</p>
      : assessment.data?.blocker && <p className="muted">{assessment.data.blocker}</p>}
    {pending && <p role="status">Waiting for confirmation of the stop request.</p>}
    <div className="actions">
      <button disabled={busy} onClick={() => void reconcile()}>Reconcile</button>
      <button disabled={!canEnd} onClick={() => { setReason(''); setReviewRevision(currentRevision ?? null); }}>End relocation</button>
      {pending && <button disabled={busy} onClick={() => void end(pending)}>Retry same stop request</button>}
    </div>
    {reviewRevision && <dialog ref={dialog} onCancel={() => setReviewRevision(null)} aria-labelledby={`stop-title-${dispatchId}`}>
      <h2 id={`stop-title-${dispatchId}`}>End relocation</h2>
      <p>Fleet will preserve the last recorded position and battery level. It will fence the old simulator session before releasing the claim. A stranded EV stays unavailable until it meets normal parking and telemetry checks.</p>
      {reviewRevision === currentRevision && assessment.data?.stop_position && <p>
        Recorded stop position {assessment.data.stop_position.latitude.toFixed(5)}, {assessment.data.stop_position.longitude.toFixed(5)}
        {assessment.data.stop_soc_pct != null && <> · Battery {assessment.data.stop_soc_pct.toFixed(1)}%</>}
      </p>}
      <label>Reason for ending the relocation <input value={reason} onChange={event => setReason(event.target.value)} maxLength={200} required /></label>
      {(!canEnd || reviewRevision !== currentRevision) && <p role="status">The stop evidence changed. Leave the relocation open and review it again.</p>}
      <div className="actions">
        <button autoFocus onClick={() => setReviewRevision(null)}>Leave open</button>
        <button className="primary" disabled={!canEnd || reviewRevision !== currentRevision || !reason.trim()}
          onClick={() => void end({ request_id: crypto.randomUUID(), expected_assessment_revision: reviewRevision, reason: reason.trim() })}>Confirm end relocation</button>
      </div>
    </dialog>}
  </div>;
}
