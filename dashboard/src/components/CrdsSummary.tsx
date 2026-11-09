import type { CrdsMetadata, CrdsVehicleDecision } from '../api/contracts';
import { crdsMessage } from './crdsMessages';
import { number } from './format';

const groupLabels = {
  RECOMMENDED: 'Recommended EVs',
  NOT_SELECTED: 'EVs not selected',
  EXCLUDED: 'Excluded EVs',
  INCOMPLETE: 'EVs with incomplete assessments',
} satisfies Record<CrdsVehicleDecision['outcome'], string>;

function decisionEvidence(result: CrdsMetadata) {
  const evidence = result.source_references.decision_evidence;
  return evidence?.version === 1 ? evidence : undefined;
}

export function CrdsSummary({ result, batchStop }: { result: CrdsMetadata; batchStop?: string | null }) {
  const evidence = decisionEvidence(result);
  const excluded = Object.values(evidence?.vehicles ?? {}).filter(row => row.outcome === 'EXCLUDED');
  const singleBlocker = excluded.length > 0 && excluded.every(row => row.reason === excluded[0].reason)
    ? excluded[0].reason : null;
  const assessment = evidence?.assessment_reasons ?? result.reason_code.split(',');
  const partialRoutes = assessment.includes('PARTIAL_ROUTE_COVERAGE');
  const batchReasons = evidence?.batch_stop_reasons.length ? evidence.batch_stop_reasons : batchStop ? [batchStop] : [];
  const messages = result.decision === 'HOLD' && assessment.includes('NO_ELIGIBLE_RELOCATIONS') && singleBlocker
    ? [...assessment, singleBlocker] : assessment.filter(reason => reason !== 'PARTIAL_ROUTE_COVERAGE');
  const stopMessages = [...new Set(batchReasons
    .filter(reason => !assessment.includes(reason) && !messages.includes(reason)).map(reason => crdsMessage(reason)))];
  const label = result.decision === 'MOVE' ? 'Recommended'
    : result.decision === 'HOLD' ? 'No move recommended' : 'Assessment unavailable';

  return <section aria-label="Fleet-wide availability assessment">
    <strong>{label}</strong>
    <p className="recommendation-reason" role={result.decision === 'DEFER' ? 'status' : undefined}>
      {result.decision === 'MOVE' && batchStop !== undefined
        ? 'The proposed moves improve the modeled fleet coverage score, even in the least favorable scenario checked.'
        : [...new Set(messages.map(code => crdsMessage(code)))].join(' ')}
    </p>
    {partialRoutes && <p className="warning" role="status">{crdsMessage('PARTIAL_ROUTE_COVERAGE')}</p>}
    {result.decision !== 'DEFER' && stopMessages.length > 0 &&
      <p className="warning" role="status">{stopMessages.join(' ')}</p>}
    {result.decision === 'MOVE' && <dl className="candidate-metrics">
      <div><dt>Minimum modeled coverage gain</dt><dd>{number(result.worst_case_gain_vehicle_minutes, ' points')}</dd></div>
      <div><dt>Arrival estimate</dt><dd>{number(result.arrival_minutes_min)}–{number(result.arrival_minutes_max)} min</dd></div>
    </dl>}
    {excluded.length > 0 && <p>{excluded.length} {excluded.length === 1 ? 'EV was' : 'EVs were'} excluded. Open Evidence to see why.</p>}
    {result.decision === 'MOVE' && !result.approval_allowed && <p className="muted">Advisory mode. This move is available for review only.</p>}
  </section>;
}

export function CrdsEvidence({ result, onRelocation }: { result: CrdsMetadata; onRelocation?: (id: string) => void }) {
  const evidence = decisionEvidence(result);
  const legacyExclusions = result.source_references.relocation_exclusions ?? [];
  const sites = result.source_references.excluded_facility_ids?.length ?? 0;
  return <section aria-label="Fleet-wide assessment evidence">
    {evidence ? Object.entries(groupLabels).map(([outcome, label]) => {
      const rows = Object.entries(evidence.vehicles).filter(([, row]) => row.outcome === outcome);
      return rows.length > 0 && <div key={outcome}>
        <h4>{label} ({rows.length})</h4>
        <ul>{rows.map(([id, row]) => <li key={id}>
          <strong>{id}</strong> · {crdsMessage(row.reason)}
          {row.related_reasons.length > 0 && <> {row.related_reasons.map(reason => crdsMessage(reason)).join(' ')}</>}
          {row.arrival_soc_pct != null && row.reserve_pct != null &&
            <> {row.arrival_soc_pct < 0 ? 'The EV is expected to run out of battery before arrival.'
              : `Estimated battery on arrival ${number(row.arrival_soc_pct, '%')}.`}
              {' '}Required reserve {number(row.reserve_pct, '%')}.</>}
          {row.gain_vehicle_minutes != null && <> Minimum modeled benefit {number(row.gain_vehicle_minutes, ' points')}.</>}
          {row.origin_shortage_increase != null &&
            <> Shortage in the origin zone increases by {number(row.origin_shortage_increase, ' vehicle-minutes')} in this scenario.</>}
          {row.dispatch_ids.map(dispatch => onRelocation &&
            <button key={dispatch} onClick={() => onRelocation(dispatch)}>View relocation for {id}</button>)}
        </li>)}</ul>
      </div>;
    }) : legacyExclusions.length > 0 && <>
      <h4>Relocations excluded from supply</h4>
      <ul>{legacyExclusions.map(row => <li key={row.vehicle_id}>
        <strong>{row.vehicle_id}</strong> · {crdsMessage(row.reason)}
        {row.dispatch_ids.map(id => onRelocation &&
          <button key={id} onClick={() => onRelocation(id)}>View relocation for {row.vehicle_id}</button>)}
      </li>)}</ul>
    </>}
    {sites > 0 && <p>CRDS excluded {sites} {sites === 1 ? 'site' : 'sites'} while destination reservations are unconfirmed.</p>}
    {result.decision !== 'DEFER' && <>
      <h4>Modeled effect on EV supply</h4>
      <p className="muted">CRDS checks {result.scenario_count} scenarios over {result.parameters.horizon_minutes} minutes and recommends moves only above {number(result.parameters.minimum_gain_vehicle_minutes, ' points')} of minimum benefit.
        {(result.parameters.priority_coverage_pct ?? 0) > 0 &&
          <> Missing EVs count double up to the first {result.parameters.priority_coverage_pct}% of demand, rounded up to a whole EV.</>}
      </p>
      {Object.keys(result.shortage_changes_by_zone).length > 0 && <>
        <p className="muted">Unweighted shortage changes in the lowest-scoring scenario. Positive values show more shortage and negative values show less.</p>
        <dl className="candidate-metrics">{Object.entries(result.shortage_changes_by_zone).map(([zone, change]) =>
          <div key={zone}><dt>{zone}</dt><dd>{number(change, ' vehicle-minutes')}</dd></div>)}</dl>
      </>}
    </>}
    <small className="identifier">Assessment reference {result.epoch_id}</small>
  </section>;
}
