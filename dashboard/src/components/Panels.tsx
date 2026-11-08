import { useEffect, useRef, useState } from 'react';
import type { Candidate, Recommendation, Vehicle, Zone } from '../api/contracts';
import type { Resource } from '../hooks/usePolling';
import { Badge, number, position, time } from './format';
import { CrdsEvidence, CrdsSummary } from './CrdsSummary';
import { OperationalTabs } from './OperationalTabs';
import { TwinHistoryPanel } from './TwinHistoryPanel';
import { SimulatorStatus } from './SimulatorStatus';

export function ServiceStatus({ name, resource }: { name: string; resource: Resource<unknown> }) {
  return <span className={`service ${resource.error ? 'broken' : resource.data ? '' : 'loading'}`}>
    <i/>{name} · {resource.error ? `unavailable (${resource.error})${resource.data ? ' · last known' : ''}` : resource.data ? 'connected' : 'connecting'}
    <small>{time(resource.updatedAt)} SGT</small>
  </span>;
}
export function ZoneDemandStatus({ zone, feedUnavailable }: { zone: Zone; feedUnavailable: boolean }) {
  if (!feedUnavailable && zone.demand_status === 'FRESH') return null;
  const status = feedUnavailable ? 'Demand feed unavailable'
    : zone.demand_status === 'STALE' ? 'Demand expired' : 'No demand observation';
  return <p className="muted demand-inbound">{status}
    {zone.rental_demand != null && <> · Last reported {number(zone.rental_demand)}
      {zone.demand_observed_at && <> at {time(zone.demand_observed_at)} SGT</>}</>}
    . Awaiting fresh demand for shortage and surplus.</p>;
}

export function ZoneMetrics({ zone, feedUnavailable, detailed = false }: {
  zone: Zone; feedUnavailable: boolean; detailed?: boolean;
}) {
  const currentDemand = !feedUnavailable && zone.demand_status === 'FRESH';
  const reading = (value: number | null) => number(feedUnavailable ? null : value);
  const unavailable = <><span aria-hidden="true">—</span><span className="visually-hidden">Unknown</span></>;
  const metric = (value: number | null) => value == null ? unavailable : number(value);
  return <>
    <dl className={detailed ? 'metrics' : 'demand-figures'}>
      <div><dt>Rental demand</dt><dd>{metric(currentDemand ? zone.rental_demand : null)}</dd></div>
      <div><dt>Available EVs</dt><dd>{metric(feedUnavailable ? null : zone.available_evs)}</dd></div>
      {detailed && <div><dt>Relocating inbound</dt><dd>{metric(feedUnavailable ? null : zone.inbound_relocating_evs)}</dd></div>}
      <div><dt>Current shortage</dt><dd>{metric(currentDemand ? zone.current_shortage : null)}</dd></div>
      <div><dt>Projected shortage</dt><dd>{metric(currentDemand ? zone.projected_shortage : null)}</dd></div>
      {detailed && <div><dt>Current / projected surplus</dt><dd>
        {zone.current_surplus == null || zone.projected_surplus == null || !currentDemand
          ? unavailable : `${number(zone.current_surplus)} / ${number(zone.projected_surplus)}`}
      </dd></div>}
    </dl>
    <ZoneDemandStatus zone={zone} feedUnavailable={feedUnavailable}/>
    <p className="muted demand-inbound">{detailed
      ? 'Projected supply includes EVs relocating in.'
      : `Relocating in: ${reading(zone.inbound_relocating_evs)}`} · Pending: {reading(zone.inbound_pending_evs)} · Unconfirmed: {reading(zone.inbound_unconfirmed_evs)}.</p>
  </>;
}

export function ZoneDetails({ zone, rentalDemandLevel, feedUnavailable }: {
  zone: Zone; rentalDemandLevel: string; feedUnavailable: boolean;
}) {
  return <><div className="section-title"><h2>{zone.name}</h2><Badge value={rentalDemandLevel}/></div>
    <p className="muted">{zone.zone_id} · {feedUnavailable ? 'UNKNOWN' : zone.data_quality}</p>
    <ZoneMetrics zone={zone} feedUnavailable={feedUnavailable} detailed/>
  </>;
}
export function CandidateDetails({ candidate }: { candidate: Candidate }) {
  return <dl className="candidate-metrics">
    <div><dt>Estimated distance</dt><dd>{number(candidate.distance_km, ' km')}</dd></div>
    <div><dt>Estimated energy</dt><dd>{number(candidate.energy_required_kwh, ' kWh')}</dd></div>
    <div><dt>SOC at recommendation</dt><dd>{number(candidate.current_soc_pct, '%')}</dd></div>
    <div><dt>Predicted arrival SOC</dt><dd>{number(candidate.predicted_arrival_soc_pct, '%')}</dd></div>
    <div><dt>{candidate.crds_gain ? 'Minimum shortage reduction' : 'Backend score'}</dt><dd>{candidate.crds_gain ? number(candidate.crds_gain.worst_case_gain_vehicle_minutes, ' vehicle-minutes') : number(candidate.score)}</dd></div>
    <div><dt>Charging assessment</dt><dd>{candidate.charging_assessment ?? 'Unknown'}</dd></div>
  </dl>;
}
export function RecommendationPanel({ recommendation, onRelocation }: { recommendation: Recommendation; onRelocation?: (id: string) => void }) {
  const candidate = recommendation.ranked_candidates.find(c => c.vehicle_id === recommendation.selected_vehicle_id);
  return <div className="recommendation" data-testid="recommendation">
    <h3>{recommendation.selected_vehicle_id ?? (recommendation.crds ? 'Relocation assessment' : 'No eligible EV')}</h3>
    {!recommendation.crds && <p className="recommendation-reason">{recommendation.explanation}</p>}
    {recommendation.crds && <CrdsSummary result={recommendation.crds}/>}
    <OperationalTabs label="Recommendation details" tabs={[
      { id: 'overview', label: 'EV assessment', content: candidate ? <CandidateDetails candidate={candidate}/> :
        <p className="muted">No vehicle was proposed. Open Evidence to inspect the assessment.</p> },
      { id: 'evidence', label: 'Evidence', content: <>
        {recommendation.crds && <CrdsEvidence result={recommendation.crds} onRelocation={onRelocation}/>}
        {recommendation.crds?.source_references.decision_evidence?.version !== 1 && <>
          <h4>Ranked vehicles</h4>
          {recommendation.ranked_candidates.length ? <ol>{recommendation.ranked_candidates.map(c => <li key={c.vehicle_id}>{c.vehicle_id} · {c.crds_gain ? `minimum gain ${number(c.crds_gain.worst_case_gain_vehicle_minutes)} vehicle-minutes` : `score ${number(c.score)}`}</li>)}</ol> : <p>No ranked vehicles.</p>}
        </>}
        {!recommendation.crds && <>
          <h4>Exclusions</h4>
          {recommendation.excluded_candidates.length ? <ul>{recommendation.excluded_candidates.map(c => <li key={c.vehicle_id}>{c.vehicle_id}: {c.exclusion_reasons.join(', ')}</li>)}</ul> : <p>No excluded vehicles.</p>}
        </>}
        {recommendation.assumptions.length > 0 && <><h4>Assumptions</h4>
          <ul>{recommendation.assumptions.map((assumption, index) => <li key={index}>{assumption}</li>)}</ul></>}
      </> },
    ]}/>
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
    <div className="actions"><button onClick={onClose} autoFocus>Cancel</button><button className="primary" disabled={expired} onClick={onApprove}>Approve 1 EV</button></div>
  </dialog>;
}
export function FleetPanel({ vehicles, initialVehicle, unparkedOnly, fleetFeedHealthy, onClearFilter }: {
  vehicles: Vehicle[]; initialVehicle?: string; unparkedOnly: boolean; fleetFeedHealthy: boolean; onClearFilter: () => void;
}) {
  const [selected, setSelected] = useState('');
  const [diagnostics, setDiagnostics] = useState(false);
  const vehicle = vehicles.find(item => item.vehicle_id === selected) ?? vehicles.find(item => item.vehicle_id === initialVehicle);
  return <div className="fleet-workspace">
    <section className="panel">
      <div className="section-title"><h2 id="fleet-overview-title" tabIndex={-1}>
        {unparkedOnly ? 'Idle EVs without parking' : 'Fleet overview'}
      </h2>{unparkedOnly ? <button type="button" className="text-button" onClick={onClearFilter}>Show all EVs</button>
        : <span className="muted">{vehicles.length} vehicles</span>}</div>
      {vehicles.length > 0 ? <div className="table-scroll" tabIndex={0} role="region" aria-label="Fleet vehicle table">
        <table><thead><tr><th>Vehicle</th><th>Location</th><th>SOC</th><th>Availability</th><th>Telemetry</th></tr></thead>
          <tbody>{vehicles.map(v => <tr key={v.vehicle_id} data-testid={`vehicle-${v.vehicle_id}`} data-selected={vehicle?.vehicle_id === v.vehicle_id}>
            <td><button className="text-button" aria-pressed={vehicle?.vehicle_id === v.vehicle_id} onClick={() => {
              setSelected(v.vehicle_id);
            }}>{v.vehicle_id}</button></td>
            <td>{v.current_zone_id ?? position(v.telemetry?.position)}</td>
            <td>{number(v.telemetry?.soc_pct, '%')}</td>
            <td><Badge value={v.effective_availability}/></td>
            <td><Badge value={v.stale ? 'STALE' : v.connectivity}/><small>Received {time(v.received_at)} SGT</small></td>
          </tr>)}</tbody>
        </table>
      </div> : <p className="empty">{unparkedOnly && fleetFeedHealthy ? 'All reporting idle EVs have parking.'
        : 'Awaiting current fleet observations.'}</p>}
    </section>
    {vehicle && <section className="panel vehicle-detail" aria-label="Selected vehicle details">
      <div className="section-title"><h2>{vehicle.vehicle_id}</h2><Badge value={vehicle.effective_availability}/></div>
      <dl className="candidate-metrics">
        <div><dt>Parking / charging</dt><dd>{vehicle.telemetry?.parked_facility_id ?? 'Unallocated'}
          {vehicle.telemetry?.charging_state === 'CHARGING' && <small>Charging (simulated) · {number(vehicle.telemetry.charging_power_kw, ' kW')}</small>}
          {vehicle.telemetry?.charging_state === 'COMPLETE' && <small>Charging complete · parked</small>}</dd></div>
        <div><dt>Rental</dt><dd>{vehicle.active_rental ? <>Rented<small>Expected return {time(vehicle.active_rental.expected_end_at)} SGT</small></> : 'No active rental'}</dd></div>
        <div><dt>Relocation</dt><dd><Badge value={vehicle.effective_relocation_state}/></dd></div>
        <div><dt>Claim</dt><dd>{vehicle.active_claim ? 'Held' : 'None reported'}</dd></div>
        <div><dt>Simulator</dt><dd><SimulatorStatus vehicle={vehicle}/></dd></div>
      </dl>
      <p className="muted">Dispatch eligibility also requires fresh telemetry and valid ownership.</p>
      <button aria-expanded={diagnostics} onClick={() => setDiagnostics(!diagnostics)}>EV digital twin diagnostics</button>
    </section>}
    {vehicle && diagnostics && <div className="vehicle-diagnostics"><TwinHistoryPanel key={vehicle.vehicle_id} vehicle={vehicle}/></div>}
  </div>;
}
