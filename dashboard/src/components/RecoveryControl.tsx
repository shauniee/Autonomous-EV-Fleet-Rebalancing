import { useEffect, useRef, useState } from 'react';
import { api, ApiError, message } from '../api/client';
import type { RecoveryAssessment, RecoveryOperation, RecoveryRequest } from '../api/contracts';
import { usePolling } from '../hooks/usePolling';

const checkpointTime = (value: string | null) => value
  ? `${new Date(value).toLocaleString('en-SG', { timeZone: 'Asia/Singapore', hour12: false })} SGT`
  : 'Unavailable';

export function RecoveryControl({ dispatchId }: { dispatchId: string }) {
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
  const [error, setError] = useState('');
  const submitting = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const data = assessment.data;
  const operation = data?.operation;
  const fresh = !!assessment.updatedAt && !assessment.error && Date.now() - Date.parse(assessment.updatedAt) < 15000;
  const active = operation?.status === 'REQUESTED' || operation?.status === 'RESTORING';
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
    setBusy(true); setError(''); setReview(null);
    remember(request);
    try {
      await api<RecoveryOperation>(path, { method: 'POST', body: JSON.stringify(request) });
      // Keep retry identity until polling confirms the persisted operation.
    } catch (failure) {
      const definitive = failure instanceof ApiError && [400, 404, 409, 422].includes(failure.status);
      if (definitive) remember(null);
      setError(`${message(failure)}. ${definitive ? 'Refresh and review the current assessment.' : 'Outcome uncertain. Reconcile status or retry the same request.'}`);
    } finally {
      submitting.current = false;
      setBusy(false); assessment.refresh();
    }
  }
  const canReview = fresh && data?.can_resume && data.supported_action === 'RESUME_SIMULATION' && !active && !pending && !busy;
  return <section aria-label="Interrupted simulation recovery">
    <p role="status"><strong>{operation ? ({ REQUESTED: 'Recovery requested', RESTORING: 'Resuming simulation',
      RECOVERED: 'Simulation recovered', BLOCKED: 'Recovery blocked' }[operation.status]) : 'Simulation recovery'}</strong>
      {operation?.reason_code && <> · {operation.reason_code}</>}</p>
    {operation?.status === 'RECOVERED' && <p className="muted">Recovery confirmed by the backend. Arrival remains a separate journey outcome.</p>}
    {data && <>
      <p className="muted">Recorded checkpoint: {checkpointTime(data.evidence.checkpoint_at)} · {data.evidence.checkpoint_source.replaceAll('_', ' ')}</p>
      {data.blockers.map(blocker => <p className="warning" key={blocker.code}>{blocker.code}: {blocker.detail}</p>)}
      {!data.blockers.length && !active && !data.can_resume && <p className="muted">{data.reason_code}</p>}
    </>}
    {assessment.error && <p className="warning">Recovery assessment unavailable: {assessment.error}. Retained status is last known.</p>}
    {error && <p className="warning" role="alert">{error}</p>}
    {pending && <p className="warning">Awaiting backend confirmation. Request {pending.request_id}.</p>}
    <div className="actions">
      <button disabled={!canReview} onClick={() => { setError(''); setReview(data!); }}>Recover interrupted simulation</button>
      {pending && <button disabled={busy} onClick={() => void submit(pending)}>Retry same recovery request</button>}
      <button onClick={assessment.refresh}>Reconcile recovery status</button>
    </div>
    {review && <dialog ref={dialog} onCancel={() => setReview(null)} aria-labelledby={`recovery-title-${dispatchId}`}>
      <div className="eyebrow">Dispatcher approval</div>
      <h2 id={`recovery-title-${dispatchId}`}>Recover interrupted simulation</h2>
      <p>Vehicle: <strong>{review.vehicle_id}</strong></p>
      <p>Recorded checkpoint: {checkpointTime(review.evidence.checkpoint_at)} · {review.evidence.checkpoint_source.replaceAll('_', ' ')}</p>
      <p>Resume the same simulated journey from its last recorded state. Unreported movement cannot be reconstructed. This action does not confirm arrival or start a stopped container.</p>
      <p className="muted">The backend rechecks ownership and checkpoint evidence before resuming.</p>
      {(!canReview || data?.assessment_revision !== review.assessment_revision) && <p className="warning">Assessment changed or unavailable. Close and review again.</p>}
      <div className="actions"><button autoFocus onClick={() => setReview(null)}>Cancel</button>
        <button className="primary" disabled={!canReview || data?.assessment_revision !== review.assessment_revision}
          onClick={() => void submit({ request_id: crypto.randomUUID(), action: 'RESUME_SIMULATION', expected_assessment_revision: review.assessment_revision })}>Resume simulation</button></div>
    </dialog>}
  </section>;
}
