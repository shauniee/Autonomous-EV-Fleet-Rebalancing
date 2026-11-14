import { useEffect, useRef } from 'react';
import type { BatchDispatch, BatchRecommendation } from '../api/contracts';
import { number, time } from './format';
import { approvalDetails, approvalLabels, batchInProgress, batchProgressLabel, journeyIssue, journeyLabel } from './approvalMessages';
import { OperationalTabs } from './OperationalTabs';
import { CoverageTradeoffWarning, CrdsEvidence, CrdsSummary } from './CrdsSummary';

export function BatchPanel({ batch, onRelocation }: { batch: BatchRecommendation; onRelocation?: (id: string) => void }) {
  return <div className="recommendation" data-testid="batch-recommendation">
    <h3>{batch.recommended_count} of {batch.requested_count} EVs {batch.crds?.planning_mode === 'FULFIL_REQUEST' ? 'planned' : 'recommended'}</h3>
    {!batch.crds && <p className="recommendation-reason">{batch.explanation}</p>}
    {batch.crds && <CrdsSummary result={batch.crds} batchStop={batch.unfulfilled_reason ?? null}/>}
    {batch.traffic_status && <p className="muted">Traffic feed: <strong>{batch.traffic_status}</strong> · Preliminary straight-corridor estimates only. Actual route is validated before each start.</p>}
    {!batch.crds && batch.unfulfilled_count > 0 && <p className="warning" role="status">
      Eligible vehicles and parking spaces could not be matched.</p>}
    <OperationalTabs label="Recommendation details" tabs={[
      { id: 'assignments', label: 'Assignments', content: batch.assignments.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="Proposed assignments"><table aria-label="Batch vehicle-to-parking assignments">
      <thead><tr><th>EV</th><th>Facility destination</th><th>Distance</th><th>Arrival SOC</th><th>Baseline ETA*</th><th>Condition ETA</th><th>{batch.crds ? batch.crds.planning_mode === 'FULFIL_REQUEST' ? 'Step coverage change' : 'Step minimum gain' : 'Score'}</th></tr></thead>
      <tbody>{batch.assignments.map(item => <tr key={item.recommendation_id}>
        <td>{item.vehicle_id}</td><td>{item.facility_name}{item.requires_charging ? ' · charging required' : ' · parking'}</td>
        <td>{number(item.distance_km, ' km')}</td><td>{number(item.predicted_arrival_soc_pct, '%')}</td>
        <td>{number(item.distance_km / 30 * 60, ' min')}</td>
        <td>{item.traffic_assessment?.estimated_duration_seconds != null ?
          <>{number(item.traffic_assessment.estimated_duration_seconds / 60, ' min')}
            {item.traffic_assessment.blocked ? ' · Route blocked' : item.traffic_assessment.delay_factor != null && item.traffic_assessment.delay_factor > 1 ? ' · delayed' : ''}
          </> : 'Unknown'}</td>
        <td>{item.crds_gain ? number(item.crds_gain.worst_case_gain_vehicle_minutes, ' points') : number(item.score)}</td>
      </tr>)}</tbody>
    </table></div> : <p className="muted">No vehicle-to-facility assignments were proposed.</p> },
      { id: 'evidence', label: 'Evidence', content: <>
    {batch.crds && <CrdsEvidence result={batch.crds} onRelocation={onRelocation}/>}
    {batch.crds ? <p className="muted">Step minima may come from different possible futures. The whole-plan gain above is calculated directly. Arrival estimates and charging delay influence stock availability; idle energy is not modeled. Final routes and benefit are revalidated before each start.</p>
      : <p className="muted">*Baseline uses the synthetic 30 km/h screening assumption; actual OneMap route and safe battery reserve are revalidated by Dispatch before each start. Traffic ETA is an estimate, not a real travel-time promise. Predicted arrival SOC currently reflects route distance, not queuing energy.</p>}
    <p className="muted">Unavailable assignments are rejected; the approved EVs and destinations stay fixed.</p>
    {batch.assumptions.length > 0 && <><h4>Assumptions</h4>
      <ul>{batch.assumptions.map((assumption, index) => <li key={index}>{assumption}</li>)}</ul></>}
    </> },
    ]}/>
  </div>;
}

export function BatchConfirmation({ batch, expired, onApprove, onClose }: {
  batch: BatchRecommendation; expired: boolean; onApprove: () => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} onCancel={onClose} aria-labelledby="batch-title">
    <h2 id="batch-title">Confirm {batch.recommended_count} EV {batch.recommended_count === 1 ? 'relocation' : 'relocations'}</h2>
    <p>{batch.target_zone_id.toUpperCase()} · {batch.unfulfilled_count} unfulfilled of {batch.requested_count} requested</p>
    {batch.crds && (batch.crds.planning_mode === 'FULFIL_REQUEST' ? <>
      <p>This plan covers {batch.recommended_count} of {batch.requested_count} requested relocations, subject to fresh safety and capacity checks. Minimum modeled coverage change: {number(batch.crds.worst_case_gain_vehicle_minutes, ' points')} across the scenarios.</p>
      {batch.crds.coverage_tradeoff && <CoverageTradeoffWarning/>}
    </> : <p>The proposed plan improves modeled fleet coverage by at least {number(batch.crds.worst_case_gain_vehicle_minutes, ' points')} across the scenarios. Each move is assessed again before it starts.</p>)}
    <ol>{batch.assignments.map(a => <li key={a.recommendation_id}>{a.vehicle_id} → {a.facility_name} · {a.requires_charging ? 'charging' : 'parking'}</li>)}</ol>
    {batch.assignments.some(a => a.requires_charging) && <p className="muted">After arrival, staff must confirm cable connection before simulated charging begins. At 80%, staff confirm disconnection before the EV returns to service.</p>}
    <p>Approve the same EVs and destinations for up to <strong>30 minutes</strong> after acceptance. Relocations start gradually in the background; temporary service failures retry automatically within this window. Each start still requires fresh safety, battery, route, policy and capacity checks.</p>
    <p className="muted">The backend rechecks each vehicle and holds a space within this simulation before claiming that EV. Some assignments may fail fresh checks. Public parking and charging spaces are not reserved.</p>
    {expired && <p className="warning">Expired or stale data. Request another recommendation.</p>}
    <div className="actions"><button autoFocus onClick={onClose}>Cancel</button><button className="primary" disabled={expired} onClick={onApprove}>Approve {batch.recommended_count} {batch.recommended_count === 1 ? 'EV' : 'EVs'}</button></div>
  </dialog>;
}

export function BatchResult({ result, onRelocation, onClose, busy, progressError }: {
  result: BatchDispatch; onRelocation?: (id: string) => void; onClose?: (recommendationId: string) => void;
  busy?: boolean; progressError?: boolean;
}) {
  const unfilled = result.requested_count - result.recommended_count;
  const queuedJob = !!result.job_status || result.status === 'QUEUED' || result.status === 'PROCESSING';
  const active = batchInProgress(result);
  const expired = result.job_status === 'EXPIRED';
  return <section className="panel" aria-label="Batch approval outcomes">
    <div className="section-title"><h2>{queuedJob ? 'Batch relocation progress' : 'Batch approval outcome'}</h2><strong>{batchProgressLabel(result)}</strong></div>
    {queuedJob && <>
      <p role="status" aria-live="polite">{result.admitted_count ?? 0} admitted · {result.completed_count ?? 0} arrived · {result.queued_count ?? 0} queued</p>
      <p className="muted">Human approval accepted. Admission confirms an EV assignment; delivery and arrival are tracked separately below.
        {active && (expired
          ? <> Existing journeys and confirmation of existing claims continue automatically. No new relocations will start. Progress updates automatically.</>
          : <> EVs start gradually, with temporary failures retried automatically. Progress updates automatically.</>)}
      </p>
      {result.expires_at && <p className="muted">New starts authorized until <time dateTime={result.expires_at}>{time(result.expires_at)} SGT</time>.
        {result.in_flight_limit != null && <> Up to {result.in_flight_limit} journeys or uncertain assignments may be active across Dispatch.</>}
      </p>}
      {result.job_status === 'EXPIRED' && <p className="warning">The approval window expired. Unstarted EVs will not begin. Any uncertain assignments still require confirmation in Relocations.</p>}
    </>}
    {progressError && <p className="warning" role="status">Progress update unavailable. Showing the last known status; checking again automatically. Your approval remains saved on Dispatch.</p>}
    <p>{queuedJob ? 'Assignments for' : 'Approval results for'} {result.items.length} of {result.requested_count} requested EVs.
      {unfilled > 0 && <> The recommendation left {unfilled} {unfilled === 1 ? 'assignment' : 'assignments'} unfilled.</>}
    </p>
    <div className="table-scroll" tabIndex={0} role="region" aria-label="Approval outcomes by EV">
      <table className="batch-approval-table"><thead><tr><th>EV and site</th><th>{queuedJob ? 'Queue and admission' : 'Approval'}</th><th>Relocation</th></tr></thead>
        <tbody>{result.items.map(item => {
          const waiting = item.queue_status === 'QUEUED' || item.queue_status === 'WAITING';
          const cancelled = item.queue_status === 'CANCELLED';
          const status = item.approval_status ?? item.approval_decision?.status
            ?? (result.status === 'APPROVED' ? 'APPROVED' : item.state === 'REJECTED' ? 'REJECTED' : 'UNKNOWN');
          const dispatchId = item.dispatch_id;
          const issue = status === 'APPROVED' ? journeyIssue(item.state, item.error) : '';
          const canClose = queuedJob ? active && waiting : !dispatchId && status === 'UNKNOWN';
          return <tr key={item.recommendation_id}>
            <td><strong>{item.vehicle_id}</strong><div className="muted">{item.facility_id}</div></td>
            <td>{waiting ? <>
              <strong>{expired ? 'Checking existing assignment' : item.queue_status === 'WAITING' ? 'Waiting · automatic retry' : 'Queued for admission'}</strong>
              <div>{expired ? 'Authorization expired. Dispatch retries confirmation of existing claims automatically; no new relocations will start.'
                : item.queue_status === 'WAITING' ? 'A temporary condition is delaying this start. Dispatch will retry automatically.' : 'Human approval saved. Fresh checks run before this EV is assigned.'}</div>
              {item.attempts != null && item.attempts > 0 && <div className="muted">Attempt {item.attempts}{item.retry_at && <> · Next check <time dateTime={item.retry_at}>{time(item.retry_at)} SGT</time></>}</div>}
            </> : <>
              <strong>{cancelled ? 'Cancelled before start' : queuedJob && status === 'APPROVED' ? 'Admitted' : approvalLabels[status]}</strong>
              <div>{queuedJob && status === 'UNKNOWN'
                ? 'Admission is unconfirmed. Dispatch is checking the existing assignment automatically.'
                : approvalDetails(status, item.approval_decision, status === 'APPROVED' ? null : item.error)}</div>
            </>}
              {canClose && onClose && <button disabled={busy} onClick={() => onClose(item.recommendation_id)}>Close unstarted approval</button>}
            </td>
            <td>{waiting ? expired ? 'Confirming existing assignment' : 'Awaiting admission' : cancelled ? 'Did not start' : status === 'APPROVED' ? <>
              <strong>{journeyLabel(item.state)}</strong>
              {issue && <div>{issue}</div>}
            </> : status === 'REJECTED' ? 'Did not start' : 'Awaiting admission confirmation'}
              {dispatchId && (!waiting || expired) && !cancelled && onRelocation && <div><button onClick={() => onRelocation(dispatchId)}>View relocation</button></div>}
            </td>
          </tr>;
        })}</tbody>
      </table>
    </div>
  </section>;
}
