import { useEffect, useRef } from 'react';
import type { BatchDispatch, BatchRecommendation } from '../api/contracts';
import { number } from './format';

export function BatchPanel({ batch, remaining, disabled, onReview }: {
  batch: BatchRecommendation; remaining: number; disabled: boolean; onReview: () => void;
}) {
  return <div className="recommendation" data-testid="batch-recommendation">
    <div className="eyebrow">Parking-aware batch recommendation</div>
    <h3>{batch.recommended_count} of {batch.requested_count} EVs recommended</h3>
    <p>{batch.explanation}</p>
    {batch.unfulfilled_count > 0 && <p className="warning" role="status">
      {batch.unfulfilled_count} requested EV(s) could not be matched to eligible vehicles and parking spaces.</p>}
    <div className="table-scroll"><table aria-label="Batch vehicle-to-parking assignments">
      <thead><tr><th>EV</th><th>Mock parking destination</th><th>Distance</th><th>Arrival SOC</th><th>Score</th></tr></thead>
      <tbody>{batch.assignments.map(item => <tr key={item.recommendation_id}>
        <td>{item.vehicle_id}</td><td>{item.facility_name}{item.requires_charging ? ' · charging required' : ''}</td>
        <td>{number(item.distance_km, ' km')}</td><td>{number(item.predicted_arrival_soc_pct, '%')}</td>
        <td>{number(item.score)}</td>
      </tr>)}</tbody>
    </table></div>
    <p className={remaining ? 'muted' : 'warning'}>{remaining ? `Expires in ${remaining}s` : 'Batch recommendation expired.'}</p>
    <p className="muted">No EV or parking space is claimed until an operator explicitly approves. Allocations may change if capacity becomes unavailable.</p>
    <button className="primary" disabled={disabled || batch.recommended_count === 0 || remaining <= 0} onClick={onReview}>Review batch dispatch</button>
  </div>;
}

export function BatchConfirmation({ batch, expired, onApprove, onClose }: {
  batch: BatchRecommendation; expired: boolean; onApprove: () => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} onCancel={onClose} aria-labelledby="batch-title">
    <div className="eyebrow">Explicit dispatcher approval</div>
    <h2 id="batch-title">Confirm {batch.recommended_count} EV relocations</h2>
    <p>{batch.target_zone_id.toUpperCase()} · {batch.unfulfilled_count} unfulfilled of {batch.requested_count} requested</p>
    <ol>{batch.assignments.map(a => <li key={a.recommendation_id}>{a.vehicle_id} → {a.facility_name}</li>)}</ol>
    <p className="muted">The backend rechecks each vehicle and atomically holds a mock parking space before claiming that EV. Partial approval is possible.</p>
    {expired && <p className="warning">Expired or stale data. Request another recommendation.</p>}
    <div className="actions"><button autoFocus onClick={onClose}>Cancel</button><button className="primary" disabled={expired} onClick={onApprove}>Approve {batch.recommended_count} EVs</button></div>
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
