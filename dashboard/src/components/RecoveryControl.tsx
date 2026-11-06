import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { RecoveryAssessment, RecoveryOperation, RecoveryRequest } from '../api/contracts';
import { usePolling } from '../hooks/usePolling';
import { RelocationResolutionControl } from './RelocationResolution';
import type { Notify } from './Notifications';

const checkpointTime = (value: string | null) => value
  ? `${new Date(value).toLocaleString('en-SG', { timeZone: 'Asia/Singapore', hour12: false })} SGT`
  : 'Unavailable';

export function RecoveryControl({ dispatchId, notify, showResolution }: { dispatchId: string; notify: Notify; showResolution?: boolean }) {
  const path = `/api/dispatch/dispatches/${encodeURIComponent(dispatchId)}/recovery`;
  const key = `fleet.pending-recovery.${dispatchId}`;
  const assessment = usePolling<RecoveryAssessment>(path, 5000);
  const [pending, setPending] = useState<RecoveryRequest | null>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
      return saved?.action === 'RESUME_SIMULATION' && typeof saved.request_id === 'string' &&
        typeof saved.expected_assessment_revision === 'string' ? saved : null;
    } catch { return null; }
  });
  const [review, setReview] = useState<RecoveryAssessment | null>(null);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const data = assessment.data;
  const operation = data?.operation;
  const fresh = !!assessment.updatedAt && !assessment.error && Date.now() - Date.parse(assessment.updatedAt) < 15000;
  const active = operation?.status === 'REQUESTED' || operation?.status === 'RESTORING';
  const recoveryOutcome = operation?.status;
  const recoveryRequestId = operation?.request_id;
  useEffect(() => {
    if (recoveryOutcome === 'RECOVERED') notify('info', 'The relocation resumed.');
    if (recoveryOutcome === 'BLOCKED') notify('warning', 'The relocation could not resume. Check its status.');
  }, [recoveryOutcome, recoveryRequestId, notify]);

  function remember(request: RecoveryRequest | null) {
    setPending(request);
    try {
      if (request) sessionStorage.setItem(key, JSON.stringify(request));
      else sessionStorage.removeItem(key);
    } catch { /* Keep the exact request in memory when storage is unavailable. */ }
  }
  useEffect(() => {
    if (pending && operation?.request_id === pending.request_id) {
      setPending(null);
      try { sessionStorage.removeItem(key); } catch { /* Backend status remains authoritative. */ }
    }
  }, [pending, operation?.request_id, key]);
  useEffect(() => { if (review) dialog.current?.showModal(); }, [review]);

  async function submit(request: RecoveryRequest) {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true); setReview(null);
    remember(request);
    try {
      await api<RecoveryOperation>(path, { method: 'POST', body: JSON.stringify(request) });
      // Keep retry identity until polling confirms the persisted operation.
    } catch (failure) {
      const definitive = failure instanceof ApiError && [400, 404, 409, 422].includes(failure.status);
      if (definitive) remember(null);
      notify(definitive ? 'error' : 'warning', definitive
        ? 'The relocation could not resume. Check its status before trying again.'
        : 'Resume request unconfirmed. Check status or retry the request.');
    } finally {
      submitting.current = false;
      setBusy(false); assessment.refresh();
    }
  }
  const canReview = fresh && data?.can_resume && data.supported_action === 'RESUME_SIMULATION' && !active && !pending && !busy;
  const recoveryStatus = operation ? ({
    REQUESTED: 'Waiting to resume',
    RESTORING: 'Resuming relocation',
    RECOVERED: 'Relocation resumed',
    BLOCKED: 'Could not resume',
  }[operation.status]) : 'Resume relocation';
  const recoveryIssue = assessment.error ? 'Could not check whether the relocation can resume.'
    : !active && data?.blockers.length ? (
      data.blockers.some(blocker => ['FACILITY_UNAVAILABLE', 'PARKING_HOLD_RELEASED'].includes(blocker.code))
        ? 'The destination is unavailable.'
        : data.blockers.some(blocker => blocker.code === 'EXECUTION_FAILED')
          ? 'This failed move cannot be resumed.'
          : 'The information needed to resume is unavailable or out of date.'
    ) : !operation && data?.status === 'BLOCKED' ? 'This relocation cannot resume right now.' : null;
  return <section className="relocation-controls" aria-label="Resume relocation">
    <p role="status"><strong>{recoveryStatus}</strong></p>
    {recoveryIssue && <p className="warning">{recoveryIssue}</p>}
    {pending && <p role="status">Waiting for confirmation of the resume request.</p>}
    <div className="actions">
      <button disabled={!canReview} onClick={() => setReview(data!)}>Review resume</button>
      {pending && <button disabled={busy} onClick={() => void submit(pending)}>Retry resume request</button>}
      {!showResolution && <button onClick={assessment.refresh}>Refresh resume status</button>}
    </div>
    {showResolution && <RelocationResolutionControl dispatchId={dispatchId} notify={notify} refreshRecovery={assessment.refresh}/>}
    {review && <dialog ref={dialog} onCancel={() => setReview(null)} aria-labelledby={`recovery-title-${dispatchId}`}>
      <h2 id={`recovery-title-${dispatchId}`}>Resume relocation</h2>
      <p>Vehicle: <strong>{review.vehicle_id}</strong></p>
      <p>Last saved update: {checkpointTime(review.evidence.checkpoint_at)}</p>
      <p>The vehicle will resume from its last saved position. Any movement since that update may be lost.</p>
      <p className="muted">The relocation can only resume if its saved information is still current.</p>
      {(!canReview || data?.assessment_revision !== review.assessment_revision) && <p className="warning">The relocation status changed or is unavailable. Close and review again.</p>}
      <div className="actions"><button autoFocus onClick={() => setReview(null)}>Cancel</button>
        <button className="primary" disabled={!canReview || data?.assessment_revision !== review.assessment_revision}
          onClick={() => void submit({ request_id: crypto.randomUUID(), action: 'RESUME_SIMULATION', expected_assessment_revision: review.assessment_revision })}>Confirm resume</button></div>
    </dialog>}
  </section>;
}
