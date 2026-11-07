import type { CrdsMetadata, CrdsRelocationExclusion } from '../api/contracts';
import { Badge, number } from './format';

const exclusionReasons: Record<CrdsRelocationExclusion['reason'], string> = {
  TELEMETRY_UNAVAILABLE: 'Current vehicle telemetry is unavailable',
  RELOCATION_FAILED: 'The relocation failed',
  RELOCATION_UNCONFIRMED: 'The relocation outcome is unconfirmed',
  RELOCATION_OWNERSHIP_UNCONFIRMED: 'The vehicle and relocation records disagree',
  VEHICLE_MISSING: 'The vehicle is missing from the fleet snapshot',
};

export function CrdsSummary({ result }: { result: CrdsMetadata }) {
  const excluded = result.source_references.relocation_exclusions?.length ?? 0;
  return <section aria-label="Fleet-wide availability assessment">
    <Badge value={result.decision}/>
    {result.decision === 'MOVE' && <dl className="candidate-metrics">
      <div><dt>Minimum modeled shortage reduction</dt><dd>{number(result.worst_case_gain_vehicle_minutes, ' vehicle-minutes')}</dd></div>
      <div><dt>Arrival estimate</dt><dd>{number(result.arrival_minutes_min)}–{number(result.arrival_minutes_max)} min</dd></div>
    </dl>}
    {result.decision === 'HOLD' && <p>{result.reason_code === 'NO_ELIGIBLE_RELOCATIONS'
      ? 'No eligible EV and destination pair is available for this request.'
      : 'Waiting is preferred within the assessed candidates and possible futures.'}</p>}
    {result.decision === 'DEFER' && <p role="status">CRDS needs trustworthy planning inputs before it can recommend a move. Check the assessment details and request a new recommendation when the inputs are current.</p>}
    {excluded > 0 && <p>{excluded} {excluded === 1 ? 'EV was' : 'EVs were'} excluded because of unresolved relocation evidence. Open Evidence for details.</p>}
    {result.decision === 'MOVE' && !result.approval_allowed && <p className="muted">Advisory mode. This move is available for review only.</p>}
    <p className="muted">The calculation compares {result.scenario_count} declared futures over {result.parameters.horizon_minutes} minutes. These are modeled outcomes, not guaranteed arrivals.</p>
  </section>;
}

export function CrdsEvidence({ result, onRelocation }: { result: CrdsMetadata; onRelocation?: (id: string) => void }) {
  const excluded = result.source_references.relocation_exclusions ?? [];
  const sites = result.source_references.excluded_facility_ids?.length ?? 0;
  return <section aria-label="Fleet-wide assessment evidence">
    <h4>Effect on zones in the minimum-benefit future</h4>
    {Object.keys(result.shortage_changes_by_zone).length > 0 ? <>
      <p className="muted">Positive values mean additional shortage. Negative values mean shortage relief.</p>
      <dl className="candidate-metrics">{Object.entries(result.shortage_changes_by_zone).map(([zone, change]) =>
        <div key={zone}><dt>{zone}</dt><dd>{number(change, ' vehicle-minutes')}</dd></div>)}</dl>
    </> : <p className="muted">No zone impact reported for this assessment.</p>}
    {excluded.length > 0 && <>
      <h4>Relocations excluded from supply</h4>
      <ul>{excluded.map(row => <li key={row.vehicle_id}>
        <strong>{row.vehicle_id}</strong> · {exclusionReasons[row.reason]}
        {onRelocation && row.dispatch_ids.map(id =>
          <button key={id} onClick={() => onRelocation(id)}>View relocation for {row.vehicle_id}</button>)}
      </li>)}</ul>
    </>}
    {sites > 0 && <p>CRDS excluded {sites} {sites === 1 ? 'site' : 'sites'} while destination reservations are unconfirmed.</p>}
    <h4>Assessment details</h4>
    <p>Reason: {result.reason_code} · Model: {result.scenario_model_version}</p>
    <p>Minimum-benefit future: {result.worst_case_future_id ?? 'Not assessed'}</p>
    <small className="identifier">Calculation {result.epoch_id} · {result.input_fingerprint}</small>
  </section>;
}
