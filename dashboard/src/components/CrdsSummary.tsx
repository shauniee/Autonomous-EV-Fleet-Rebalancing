import type { CrdsMetadata } from '../api/contracts';
import { Badge, number } from './format';

export function CrdsSummary({ result }: { result: CrdsMetadata }) {
  return <section aria-label="Fleet-wide availability assessment">
    <Badge value={result.decision}/>
    {result.decision === 'MOVE' && <dl className="candidate-metrics">
      <div><dt>Minimum modeled shortage reduction</dt><dd>{number(result.worst_case_gain_vehicle_minutes, ' vehicle-minutes')}</dd></div>
      <div><dt>Arrival estimate</dt><dd>{number(result.arrival_minutes_min)}–{number(result.arrival_minutes_max)} min</dd></div>
    </dl>}
    {result.decision === 'HOLD' && <p>Waiting is preferred within the assessed candidates and possible futures.</p>}
    {result.decision === 'DEFER' && <p role="status">The assessment is incomplete. Refresh the observations and request a new recommendation.</p>}
    {!result.approval_allowed && <p className="muted">This assessment cannot be approved. Advisory mode provides recommendations for review only.</p>}
    <p className="muted">The calculation compares {result.scenario_count} declared futures over {result.parameters.horizon_minutes} minutes. These are modeled outcomes, not guaranteed arrivals.</p>
  </section>;
}

export function CrdsEvidence({ result }: { result: CrdsMetadata }) {
  return <section aria-label="Fleet-wide assessment evidence">
    <h4>Effect on zones in the minimum-benefit future</h4>
    {Object.keys(result.shortage_changes_by_zone).length > 0 ? <>
      <p className="muted">Positive values mean additional shortage. Negative values mean shortage relief.</p>
      <dl className="candidate-metrics">{Object.entries(result.shortage_changes_by_zone).map(([zone, change]) =>
        <div key={zone}><dt>{zone}</dt><dd>{number(change, ' vehicle-minutes')}</dd></div>)}</dl>
    </> : <p className="muted">No zone impact reported for this assessment.</p>}
    <h4>Assessment details</h4>
    <p>Reason: {result.reason_code} · Model: {result.scenario_model_version}</p>
    <p>Minimum-benefit future: {result.worst_case_future_id ?? 'Not assessed'}</p>
    <small className="identifier">Calculation {result.epoch_id} · {result.input_fingerprint}</small>
  </section>;
}
