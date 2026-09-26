import { useEffect, useRef } from 'react';
import type { Candidate, Claim, Dispatch, Recommendation, Vehicle, Zone } from '../api/contracts';
import { usePolling, type Resource } from '../hooks/usePolling';
import { Badge, number, position, time } from './format';

export function ServiceStatus({ name, resource }: { name: string; resource: Resource<unknown> }) {
  return <span className={`service ${resource.error ? 'broken' : resource.data ? '' : 'loading'}`}>
    <i/>{name} · {resource.error ? `unavailable (${resource.error})${resource.data ? ' · last known' : ''}` : resource.data ? 'connected' : 'connecting'}
    <small>{time(resource.updatedAt)} SGT</small>
  </span>;
}
export function ZoneDetails({ zone }: { zone: Zone }) {
  return <><div className="section-title"><h2>{zone.name}</h2><Badge value={zone.hotspot_severity}/></div>
    <p className="muted">{zone.zone_id} · Demand {zone.demand_level} · {zone.data_quality}</p>
    <dl className="metrics">
      <div><dt>Rental demand</dt><dd>{number(zone.rental_demand)}</dd></div>
      <div><dt>Available EVs</dt><dd>{number(zone.available_evs)}</dd></div>
      <div><dt>Relocating inbound</dt><dd>{number(zone.inbound_relocating_evs)}</dd></div>
      <div><dt>Current shortage</dt><dd>{number(zone.current_shortage)}</dd></div>
      <div><dt>Projected shortage</dt><dd>{number(zone.projected_shortage)}</dd></div>
      <div><dt>Current / projected surplus</dt><dd>{number(zone.current_surplus)} / {number(zone.projected_surplus)}</dd></div>
    </dl><p className="muted">Inbound supply is projected, not currently available. Pending: {number(zone.inbound_pending_evs)} · Unconfirmed: {number(zone.inbound_unconfirmed_evs)}.</p></>;
}
export function CandidateDetails({ candidate }: { candidate: Candidate }) {
  return <dl className="candidate-metrics">
    <div><dt>Estimated distance</dt><dd>{number(candidate.distance_km, ' km')}</dd></div>
    <div><dt>Estimated energy</dt><dd>{number(candidate.energy_required_kwh, ' kWh')}</dd></div>
    <div><dt>SOC at recommendation</dt><dd>{number(candidate.current_soc_pct, '%')}</dd></div>
    <div><dt>Predicted arrival SOC</dt><dd>{number(candidate.predicted_arrival_soc_pct, '%')}</dd></div>
    <div><dt>Backend score</dt><dd>{number(candidate.score)}</dd></div>
    <div><dt>Charging assessment</dt><dd>{candidate.charging_assessment ?? 'Unknown'}</dd></div>
  </dl>;
}
export function RecommendationPanel({ recommendation, remaining, onReview, disabled }: {
  recommendation: Recommendation; remaining: number; onReview: () => void; disabled: boolean;
}) {
  const candidate = recommendation.ranked_candidates.find(c => c.vehicle_id === recommendation.selected_vehicle_id);
  return <div className="recommendation" data-testid="recommendation">
    <div className="eyebrow">Backend recommendation</div><h3>{recommendation.selected_vehicle_id ?? 'No eligible EV'}</h3>
    <p>{recommendation.explanation}</p>{candidate && <CandidateDetails candidate={candidate}/>}
    <p className={remaining > 0 ? 'muted' : 'warning'}>{remaining > 0 ? `Expires in ${remaining}s` : 'Recommendation expired. Request a new recommendation.'}</p>
    <button className="primary" disabled={disabled || !candidate || remaining <= 0} onClick={onReview}>Review dispatch</button>
    <details><summary>Ranking, exclusions & assumptions</summary>
      <ol>{recommendation.ranked_candidates.map(c => <li key={c.vehicle_id}>{c.vehicle_id} · score {number(c.score)}</li>)}</ol>
      {recommendation.excluded_candidates.map(c => <p key={c.vehicle_id}>{c.vehicle_id}: {c.exclusion_reasons.join(', ')}</p>)}
      {recommendation.excluded_candidates.length === 0 && <p>No excluded vehicles.</p>}
      {recommendation.assumptions.map((text, i) => <p key={i}>{text}</p>)}
    </details>
  </div>;
}
export function DispatchConfirmation({ recommendation, vehicle, expired, onClose, onApprove }: {
  recommendation: Recommendation; vehicle?: Vehicle; expired: boolean; onClose: () => void; onApprove: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  const candidate = recommendation.ranked_candidates.find(c => c.vehicle_id === recommendation.selected_vehicle_id);
  return <dialog ref={dialog} onCancel={onClose} aria-labelledby="confirmation-title">
    <div className="eyebrow">Dispatcher approval</div><h2 id="confirmation-title">Confirm relocation</h2>
    <p>This approval sends a relocation command to the selected simulated EV.</p>
    <dl className="candidate-metrics">
      <div><dt>Selected EV</dt><dd>{recommendation.selected_vehicle_id}</dd></div>
      <div><dt>Origin (latest snapshot)</dt><dd>{vehicle?.current_zone_id || position(vehicle?.telemetry?.position)}</dd></div>
      <div><dt>Destination</dt><dd>{recommendation.target.name}</dd></div>
      <div><dt>Current SOC (latest snapshot)</dt><dd>{number(vehicle?.telemetry?.soc_pct, '%')}</dd></div>
      <div><dt>Predicted arrival SOC (recommendation)</dt><dd>{number(candidate?.predicted_arrival_soc_pct, '%')}</dd></div>
    </dl><p className="muted">The backend revalidates this recommendation before claiming the vehicle.</p>
    {expired && <p role="alert" className="warning">Recommendation expired or current data unavailable. Close and refresh before approval.</p>}
    <div className="actions"><button onClick={onClose} autoFocus>Cancel</button><button className="primary" disabled={expired} onClick={onApprove}>Approve Dispatch</button></div>
  </dialog>;
}
export function FleetPanel({ vehicles }: { vehicles: Vehicle[] }) {
  return <section className="panel"><div className="section-title"><h2>Fleet overview</h2><span className="muted">{vehicles.length} vehicles</span></div>
    <div className="table-scroll"><table><thead><tr><th>Vehicle</th><th>Zone / coordinates</th><th>SOC</th><th>Availability</th><th>Relocation</th><th>Telemetry</th></tr></thead>
      <tbody>{vehicles.map(v => <tr key={v.vehicle_id} data-testid={`vehicle-${v.vehicle_id}`}><td><strong>{v.vehicle_id}</strong></td><td>{v.current_zone_id ?? position(v.telemetry?.position)}</td><td>{number(v.telemetry?.soc_pct, '%')}</td><td>{v.effective_availability}</td><td>{v.effective_relocation_state}</td><td><Badge value={v.stale ? 'STALE' : v.connectivity}/><small>Received {time(v.received_at)}</small></td></tr>)}</tbody>
    </table></div>{vehicles.length === 0 && <p className="empty">No fleet observations available.</p>}</section>;
}
function DispatchRow({ dispatch, vehicle }: { dispatch: Dispatch; vehicle?: Vehicle }) {
  const claim = usePolling<Claim>(`/api/fleet/vehicles/${encodeURIComponent(dispatch.vehicle_id!)}/claims/${dispatch.dispatch_id}`, 10000);
  return <article className="dispatch-card" data-testid={`dispatch-${dispatch.dispatch_id}`}>
    <div className="section-title"><h3>{dispatch.vehicle_id ?? 'Unassigned EV'}</h3><Badge value={dispatch.state}/></div>
    <p>{position(claim.data?.origin)} → {dispatch.command?.target_zone_id ?? 'Unknown destination'}</p>
    {claim.error && <small className="warning">Origin receipt unavailable{claim.data ? ' · last known origin shown' : ''}</small>}
    <dl className="candidate-metrics"><div><dt>Distance travelled</dt><dd>{number(dispatch.distance_travelled_km, ' km')}</dd></div>
      <div><dt>Initial estimate</dt><dd>{number(dispatch.revalidated_metrics?.distance_km, ' km')}</dd></div>
      <div><dt>Current SOC {vehicle?.stale ? '(stale)' : ''}</dt><dd>{number(vehicle?.telemetry?.soc_pct, '%')}</dd></div>
      <div><dt>Acknowledged / execution confirmed</dt><dd>{time(dispatch.acknowledged_at)}</dd></div>
      <div><dt>Last dispatch contact</dt><dd>{time(dispatch.last_contact_at)}</dd></div>
      <div><dt>Claim held</dt><dd>{dispatch.claim_held == null ? 'Unknown' : dispatch.claim_held ? 'Yes' : 'No'}</dd></div>
    </dl>
    {dispatch.failure_reason && <p className="warning" role="status">{dispatch.failure_reason}</p>}
    {dispatch.state === 'UNKNOWN' && <p className="warning">Outcome uncertain. The backend retains ownership until reconciled.</p>}
    <small className="identifier">{dispatch.dispatch_id}</small>
  </article>;
}
export function RelocationPanel({ dispatches, vehicles }: { dispatches: Dispatch[]; vehicles: Vehicle[] }) {
  return <section className="panel"><div className="section-title"><h2>Relocations</h2><span className="muted">Live status & recent outcomes</span></div>
    {dispatches.length === 0 && <p className="empty">No dispatches yet. Choose a zone and request a recommendation to begin.</p>}
    <div className="dispatch-grid">{[...dispatches].reverse().map(d => d.vehicle_id ? <DispatchRow key={d.dispatch_id} dispatch={d} vehicle={vehicles.find(v => v.vehicle_id === d.vehicle_id)}/> : <article key={d.dispatch_id}><Badge value={d.state}/><p>{d.failure_reason}</p><small>{d.dispatch_id}</small></article>)}</div>
  </section>;
}
