import { useEffect, useRef } from 'react';
import type { BatchDispatch, BatchRecommendation } from '../api/contracts';
import { number } from './format';
import { CrdsSummary } from './CrdsSummary';

export function BatchPanel({ batch, remaining, disabled, onReview }: {
  batch: BatchRecommendation; remaining: number; disabled: boolean; onReview: () => void;
}) {
  return <div className="recommendation" data-testid="batch-recommendation">
    <div className="eyebrow">{batch.purpose === 'CHARGE' ? 'Charging destination recommendation' : 'Automatic facility recommendation'}</div>
    <h3>{batch.recommended_count} of {batch.requested_count} EVs recommended</h3>
    <p>{batch.explanation}</p>
    {batch.crds && <CrdsSummary result={batch.crds}/>}
    {batch.traffic_status && <p className="muted">Traffic feed: <strong>{batch.traffic_status}</strong> · Preliminary straight-corridor estimates only. Actual route is validated on approval.</p>}
    {batch.unfulfilled_count > 0 && <p className="warning" role="status">
      {batch.unfulfilled_count} requested EV(s) remain unfulfilled. {batch.crds
        ? batch.unfulfilled_reason === 'NO_USEFUL_MOVE' ? 'Additional moves do not provide enough modeled fleet-wide benefit.'
          : batch.unfulfilled_reason === 'CAPACITY_OR_STOCK_LIMIT' ? 'Remaining parking, charging or donor stock limits this batch.'
          : 'A complete supported assignment is not available. Review the assessment details.'
        : 'Eligible vehicles and parking spaces could not be matched.'}</p>}
    <div className="table-scroll"><table aria-label="Batch vehicle-to-parking assignments">
      <thead><tr><th>EV</th><th>Facility destination</th><th>Distance</th><th>Arrival SOC</th><th>Baseline ETA*</th><th>Condition ETA</th><th>{batch.crds ? 'Step minimum gain' : 'Score'}</th></tr></thead>
      <tbody>{batch.assignments.map(item => <tr key={item.recommendation_id}>
        <td>{item.vehicle_id}</td><td>{item.facility_name}{item.requires_charging ? ' · charging required' : ' · parking'}</td>
        <td>{number(item.distance_km, ' km')}</td><td>{number(item.predicted_arrival_soc_pct, '%')}</td>
        <td>{number(item.distance_km / 30 * 60, ' min')}</td>
        <td>{item.traffic_assessment?.estimated_duration_seconds != null ?
          <>{number(item.traffic_assessment.estimated_duration_seconds / 60, ' min')}
            {item.traffic_assessment.blocked ? ' · BLOCKED' : item.traffic_assessment.delay_factor != null && item.traffic_assessment.delay_factor > 1 ? ' · delayed' : ''}
          </> : 'Unknown'}</td>
        <td>{item.crds_gain ? number(item.crds_gain.worst_case_gain_vehicle_minutes, ' vehicle-minutes') : number(item.score)}</td>
      </tr>)}</tbody>
    </table></div>
    {batch.crds ? <p className="muted">Step minima may come from different possible futures. The whole-plan gain above is calculated directly. Arrival estimates and charging delay influence stock availability; idle energy is not modeled. Final routes and benefit are revalidated on approval.</p>
      : <p className="muted">*Baseline uses the synthetic 30 km/h screening assumption; actual OneMap route and safe battery reserve are revalidated by Dispatch at approval. Traffic ETA is an estimate, not a real travel-time promise. Predicted arrival SOC currently reflects route distance, not queuing energy.</p>}
    <p className={remaining ? 'muted' : 'warning'}>{remaining ? `Expires in ${remaining}s` : 'Batch recommendation expired.'}</p>
    <p className="muted">No EV or parking space is claimed until an operator explicitly approves. Allocations may change if capacity becomes unavailable.</p>
    <button className="primary" disabled={disabled || batch.recommended_count === 0 || remaining <= 0 || (!!batch.crds && (batch.crds.decision !== 'MOVE' || !batch.crds.approval_allowed))} onClick={onReview}>Review dispatch</button>
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
    {batch.crds && <p>The proposed plan reduces modeled shortage by at least {number(batch.crds.worst_case_gain_vehicle_minutes)} vehicle-minutes across the declared futures. Each approved move is assessed again against the already accepted moves.</p>}
    <ol>{batch.assignments.map(a => <li key={a.recommendation_id}>{a.vehicle_id} → {a.facility_name} · {a.requires_charging ? 'charging' : 'parking'}</li>)}</ol>
    {batch.assignments.some(a => a.requires_charging) && <p className="muted">Ground-team cable connection is assumed after arrival. Charging is simulated automatically to 80%; there is no separate plug-in confirmation.</p>}
    <p className="muted">The backend rechecks each vehicle and holds a space within this simulation before claiming that EV. Partial approval is possible. Public parking and charging spaces are not reserved.</p>
    {expired && <p className="warning">Expired or stale data. Request another recommendation.</p>}
    <div className="actions"><button autoFocus onClick={onClose}>Cancel</button><button className="primary" disabled={expired} onClick={onApprove}>Approve {batch.recommended_count} {batch.recommended_count === 1 ? 'EV' : 'EVs'}</button></div>
  </dialog>;
}

export function BatchResult({ result }: { result: BatchDispatch }) {
  return <section className="panel" aria-label="Batch approval outcomes">
    <div className="section-title"><h2>Batch approval outcome</h2><strong>{result.status}</strong></div>
    <p>{result.items.length} attempted assignments out of {result.requested_count} requested. See Relocations for confirmed execution and arrivals.</p>
    <div className="table-scroll"><table><thead><tr><th>EV</th><th>Facility</th><th>Approval state</th><th>Details</th></tr></thead>
      <tbody>{result.items.map(item => <tr key={item.recommendation_id}>
        <td>{item.vehicle_id}</td><td>{item.facility_id}</td><td>{item.state}</td><td>{item.error || '—'}</td>
      </tr>)}</tbody>
    </table></div>
  </section>;
}
