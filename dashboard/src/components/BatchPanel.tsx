import { useEffect, useRef } from 'react';
import type { BatchDispatch, BatchRecommendation } from '../api/contracts';
import { number } from './format';
import { approvalDetails, approvalLabels, batchApprovalLabels, journeyIssue, journeyLabel } from './approvalMessages';
import { OperationalTabs } from './OperationalTabs';
import { CrdsEvidence, CrdsSummary } from './CrdsSummary';
import { benefitPhrase, minutesText } from './crdsMessages';

export function BatchPanel({ batch, onRelocation }: { batch: BatchRecommendation; onRelocation?: (id: string) => void }) {
  return <div className="recommendation" data-testid="batch-recommendation">
    <h3>{batch.recommended_count} of {batch.requested_count} EVs recommended</h3>
    {!batch.crds && <p className="recommendation-reason">{batch.explanation}</p>}
    {batch.crds && <CrdsSummary result={batch.crds} batchStop={batch.unfulfilled_reason ?? null}/>}
    {batch.traffic_status && <p className="muted">Traffic feed: <strong>{batch.traffic_status}</strong> · Preliminary straight-corridor estimates only. Actual route is validated on approval.</p>}
    {!batch.crds && batch.unfulfilled_count > 0 && <p className="warning" role="status">
      Eligible vehicles and parking spaces could not be matched.</p>}
    <OperationalTabs label="Recommendation details" tabs={[
      { id: 'assignments', label: 'Assignments', content: batch.assignments.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="Proposed assignments"><table aria-label="Batch vehicle-to-parking assignments">
      <thead><tr><th>EV</th><th>Facility destination</th><th>Distance</th><th>Arrival SOC</th><th>Baseline ETA*</th><th>Condition ETA</th><th>{batch.crds ? 'Extra EV availability' : 'Score'}</th></tr></thead>
      <tbody>{batch.assignments.map(item => <tr key={item.recommendation_id}>
        <td>{item.vehicle_id}</td><td>{item.facility_name}{item.requires_charging ? ' · charging required' : ' · parking'}</td>
        <td>{number(item.distance_km, ' km')}</td><td>{number(item.predicted_arrival_soc_pct, '%')}</td>
        <td>{number(item.distance_km / 30 * 60, ' min')}</td>
        <td>{item.traffic_assessment?.estimated_duration_seconds != null ?
          <>{number(item.traffic_assessment.estimated_duration_seconds / 60, ' min')}
            {item.traffic_assessment.blocked ? ' · Route blocked' : item.traffic_assessment.delay_factor != null && item.traffic_assessment.delay_factor > 1 ? ' · delayed' : ''}
          </> : 'Unknown'}</td>
        <td>{item.crds_gain ? minutesText(item.crds_gain.worst_case_gain_vehicle_minutes) : number(item.score)}</td>
      </tr>)}</tbody>
    </table></div> : <p className="muted">No vehicle-to-facility assignments were proposed.</p> },
      { id: 'evidence', label: 'Evidence', content: <>
    {batch.crds && <CrdsEvidence result={batch.crds} onRelocation={onRelocation}/>}
    {batch.crds ? <p className="muted">Step minima may come from different possible futures. The whole-plan gain above is calculated directly. Arrival estimates and charging delay influence stock availability; idle energy is not modeled. Final routes and benefit are revalidated on approval.</p>
      : <p className="muted">*Baseline uses the synthetic 30 km/h screening assumption; actual OneMap route and safe battery reserve are revalidated by Dispatch at approval. Traffic ETA is an estimate, not a real travel-time promise. Predicted arrival SOC currently reflects route distance, not queuing energy.</p>}
    <p className="muted">Allocations may change if capacity becomes unavailable.</p>
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
    <div className="eyebrow">Explicit dispatcher approval</div>
    <h2 id="batch-title">Confirm {batch.recommended_count} EV {batch.recommended_count === 1 ? 'relocation' : 'relocations'}</h2>
    <p>{batch.target_zone_id.toUpperCase()} · {batch.unfulfilled_count} unfulfilled of {batch.requested_count} requested</p>
    {batch.crds?.worst_case_gain_vehicle_minutes != null && <p>Together these moves give customers {benefitPhrase(batch.crds.worst_case_gain_vehicle_minutes, batch.crds.parameters.horizon_minutes, true)}, even in the busiest demand pattern checked. Each move is checked again on approval.</p>}
    <ol>{batch.assignments.map(a => <li key={a.recommendation_id}>{a.vehicle_id} → {a.facility_name} · {a.requires_charging ? 'charging' : 'parking'}</li>)}</ol>
    {batch.assignments.some(a => a.requires_charging) && <p className="muted">Ground-team cable connection is assumed after arrival. Charging is simulated automatically to 80%; there is no separate plug-in confirmation.</p>}
    <p className="muted">The backend rechecks each vehicle and holds a space within this simulation before claiming that EV. Partial approval is possible. Public parking and charging spaces are not reserved.</p>
    {expired && <p className="warning">Expired or stale data. Request another recommendation.</p>}
    <div className="actions"><button autoFocus onClick={onClose}>Cancel</button><button className="primary" disabled={expired} onClick={onApprove}>Approve {batch.recommended_count} {batch.recommended_count === 1 ? 'EV' : 'EVs'}</button></div>
  </dialog>;
}

export function BatchResult({ result, onRelocation, onClose, busy }: {
  result: BatchDispatch; onRelocation?: (id: string) => void; onClose?: (recommendationId: string) => void; busy?: boolean;
}) {
  const unfilled = result.requested_count - result.recommended_count;
  return <section className="panel" aria-label="Batch approval outcomes">
    <div className="section-title"><h2>Batch approval outcome</h2><strong>{batchApprovalLabels[result.status]}</strong></div>
    <p>Approval results for {result.items.length} of {result.requested_count} requested EVs.
      {unfilled > 0 && <> The recommendation left {unfilled} {unfilled === 1 ? 'assignment' : 'assignments'} unfilled.</>}
    </p>
    <div className="table-scroll" tabIndex={0} role="region" aria-label="Approval outcomes by EV">
      <table className="batch-approval-table"><thead><tr><th>EV and site</th><th>Approval</th><th>Relocation</th></tr></thead>
        <tbody>{result.items.map(item => {
          const status = item.approval_status ?? item.approval_decision?.status
            ?? (result.status === 'APPROVED' ? 'APPROVED' : item.state === 'REJECTED' ? 'REJECTED' : 'UNKNOWN');
          const dispatchId = item.dispatch_id;
          const issue = status === 'APPROVED' ? journeyIssue(item.state, item.error) : '';
          return <tr key={item.recommendation_id}>
            <td><strong>{item.vehicle_id}</strong><div className="muted">{item.facility_id}</div></td>
            <td><strong>{approvalLabels[status]}</strong>
              <div>{approvalDetails(status, item.approval_decision, status === 'APPROVED' ? null : item.error)}</div>
              {!dispatchId && status === 'UNKNOWN' && onClose &&
                <button disabled={busy} onClick={() => onClose(item.recommendation_id)}>Close unstarted approval</button>}
            </td>
            <td>{status === 'APPROVED' ? <>
              <strong>{journeyLabel(item.state)}</strong>
              {issue && <div>{issue}</div>}
            </> : status === 'REJECTED' ? 'Did not start' : 'Awaiting approval confirmation'}
              {dispatchId && onRelocation && <div><button onClick={() => onRelocation(dispatchId)}>View relocation</button></div>}
            </td>
          </tr>;
        })}</tbody>
      </table>
    </div>
  </section>;
}
