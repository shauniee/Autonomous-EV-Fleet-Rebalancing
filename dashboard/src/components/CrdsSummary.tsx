import type { CrdsMetadata, CrdsVehicleDecision } from '../api/contracts';
import { benefitPhrase, crdsMessage, exclusionSummary, minutesText } from './crdsMessages';
import { number } from './format';

const groupLabels = {
  RECOMMENDED: 'Recommended EVs',
  NOT_SELECTED: 'EVs not selected',
  EXCLUDED: 'EVs that could not be considered',
  INCOMPLETE: 'EVs with incomplete assessments',
} satisfies Record<CrdsVehicleDecision['outcome'], string>;

const groupNotes = {
  RECOMMENDED: 'These EVs passed every check and help customers the most.',
  NOT_SELECTED: 'These EVs were free and able to make the trip, but CRDS did not pick them.',
  EXCLUDED: 'These EVs were ruled out before CRDS compared options.',
  INCOMPLETE: 'CRDS could not finish checking these EVs.',
} satisfies Record<CrdsVehicleDecision['outcome'], string>;

const tiebreaks = ['HIGHER_ENERGY_TIEBREAK', 'STABLE_ORDER_TIEBREAK'];

// Gain is net of what the origin zone loses, so a non-positive gain means the move hurts or does nothing.
function notUsefulReason(row: CrdsVehicleDecision) {
  const gain = row.gain_vehicle_minutes;
  if (gain == null) return crdsMessage(row.reason);
  if (gain > 0) return `Moving it would only add ${minutesText(gain)} of EV availability overall, which is not enough to be worth the trip.`;
  if (row.origin_shortage_increase) return `Moving it would do more harm than good: customers in its current zone would be left without a car for ${minutesText(row.origin_shortage_increase)}, which outweighs any benefit at the destination.`;
  return 'Moving it would not help: the destination is expected to have enough cars, or this EV would arrive too late to make a difference.';
}

function decisionEvidence(result: CrdsMetadata) {
  const evidence = result.source_references.decision_evidence;
  return evidence?.version === 1 ? evidence : undefined;
}

function headline(result: CrdsMetadata, vehicles: [string, CrdsVehicleDecision][],
  target: string | undefined, vehicle: string | null | undefined, batch: boolean) {
  const horizon = result.parameters.horizon_minutes;
  const patterns = `even in the busiest of the ${result.scenario_count} demand patterns CRDS checked`;
  const gain = result.worst_case_gain_vehicle_minutes;
  const where = target ? ` in ${target}` : '';
  if (result.decision === 'MOVE' && gain != null && batch) {
    return `Together, the proposed moves give customers${where} ${benefitPhrase(gain, horizon, true)}, ${patterns}.`;
  }
  if (result.decision === 'MOVE' && gain != null && vehicle) {
    const origin = vehicles.find(([id]) => id === vehicle)?.[1].origin_shortage_increase;
    return [`Move ${vehicle} to ${target ?? 'the selected zone'}.`,
      `Customers there would have ${benefitPhrase(gain, horizon)}, ${patterns}.`,
      origin ? `Its current zone may be short of a car for ${minutesText(origin)} as a result; that is already counted against this move.` : '',
    ].filter(Boolean).join(' ');
  }
  if (result.decision === 'HOLD') {
    const best = vehicles.filter(([, row]) => row.outcome === 'NOT_SELECTED' && row.gain_vehicle_minutes != null)
      .sort((a, b) => b[1].gain_vehicle_minutes! - a[1].gain_vehicle_minutes!)[0];
    if (best) {
      return `The best option, ${best[0]}, would give customers${where} ${benefitPhrase(best[1].gain_vehicle_minutes!, horizon)}. `
        + `CRDS only recommends a trip that adds more than ${minutesText(result.parameters.minimum_gain_vehicle_minutes)} of availability, so it is better to wait.`;
    }
  }
  return null;
}

export function CrdsSummary({ result, batchStop, target, vehicle }: {
  result: CrdsMetadata; batchStop?: string | null; target?: string; vehicle?: string | null;
}) {
  const evidence = decisionEvidence(result);
  const vehicles = Object.entries(evidence?.vehicles ?? {});
  const excluded = vehicles.map(([, row]) => row).filter(row => row.outcome === 'EXCLUDED');
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
  const { horizon_minutes: horizon, minimum_gain_vehicle_minutes: minimum } = result.parameters;

  return <section aria-label="Fleet-wide availability assessment">
    <strong>{label}</strong>
    <p className="recommendation-reason" role={result.decision === 'DEFER' ? 'status' : undefined}>
      {headline(result, vehicles, target, vehicle, batchStop !== undefined)
        ?? [...new Set(messages.map(code => crdsMessage(code)))].join(' ')}
    </p>
    {partialRoutes && <p className="warning" role="status">{crdsMessage('PARTIAL_ROUTE_COVERAGE')}</p>}
    {result.decision !== 'DEFER' && stopMessages.length > 0 &&
      <p className="warning" role="status">{stopMessages.join(' ')}</p>}
    {result.decision === 'MOVE' && <dl className="candidate-metrics">
      <div><dt>Extra EV availability</dt><dd>{result.worst_case_gain_vehicle_minutes == null ? 'Unknown'
        : minutesText(result.worst_case_gain_vehicle_minutes)}</dd></div>
      <div><dt>Arrival estimate</dt><dd>{number(result.arrival_minutes_min)}–{number(result.arrival_minutes_max)} min</dd></div>
    </dl>}
    {excluded.length > 0 && <p>{excluded.length} {excluded.length === 1 ? 'EV' : 'EVs'} could not be considered: {exclusionSummary(excluded.map(row => row.reason))}. Open Evidence for details.</p>}
    {result.decision === 'MOVE' && !result.approval_allowed && <p className="muted">Advisory mode. This move is available for review only.</p>}
    <details>
      <summary>How does CRDS decide?</summary>
      <ol>
        <li><strong>Purpose.</strong> CRDS moves idle EVs to where customers are expected to need them over the next {horizon} minutes, so fewer customers find no car.</li>
        <li><strong>Safety checks first.</strong> An EV is only considered if it is free (not rented, charging, or on another job), online with recent updates, able to reach the destination on a real road route with enough battery to spare, and there is a parking or charging space waiting for it.</li>
        <li><strong>Pick the most helpful EV.</strong> CRDS tries {result.scenario_count} possible demand patterns and judges each move by the least favourable one, so it never relies on optimistic demand. Taking an EV away from a zone that also needs it counts against the move.</li>
        <li><strong>Only move when it is worth it.</strong> A move must add more than {minutesText(minimum)} of EV availability. Otherwise CRDS recommends waiting.</li>
        <li><strong>You stay in control.</strong> Nothing moves until an operator approves, and Dispatch checks the route, battery and benefit again at approval.</li>
      </ol>
    </details>
  </section>;
}

export function CrdsEvidence({ result, onRelocation }: { result: CrdsMetadata; onRelocation?: (id: string) => void }) {
  const evidence = decisionEvidence(result);
  const legacyExclusions = result.source_references.relocation_exclusions ?? [];
  const sites = result.source_references.excluded_facility_ids?.length ?? 0;
  const winner = Object.entries(evidence?.vehicles ?? {}).find(([, row]) => row.outcome === 'RECOMMENDED');
  return <section aria-label="Fleet-wide assessment evidence">
    {evidence ? Object.entries(groupLabels).map(([outcome, label]) => {
      const rows = Object.entries(evidence.vehicles).filter(([, row]) => row.outcome === outcome)
        .sort((a, b) => (b[1].gain_vehicle_minutes ?? -Infinity) - (a[1].gain_vehicle_minutes ?? -Infinity));
      return rows.length > 0 && <div key={outcome}>
        <h4>{label} ({rows.length})</h4>
        <p className="muted">{groupNotes[outcome as CrdsVehicleDecision['outcome']]}</p>
        <ul>{rows.map(([id, row]) => {
          const compared = winner && row.reason === 'LOWER_MODELED_BENEFIT'
            && row.gain_vehicle_minutes != null && winner[1].gain_vehicle_minutes != null;
          if (row.reason === 'NO_USEFUL_MOVE') return <li key={id}>
            <strong>{id}</strong> · {notUsefulReason(row)}
            {row.dispatch_ids.map(dispatch => onRelocation &&
              <button key={dispatch} onClick={() => onRelocation(dispatch)}>View relocation for {id}</button>)}
          </li>;
          return <li key={id}>
            <strong>{id}</strong> · {crdsMessage(row.reason)}
            {row.related_reasons.length > 0 && <> {row.related_reasons.map(reason => crdsMessage(reason)).join(' ')}</>}
            {row.arrival_soc_pct != null && row.reserve_pct != null &&
              <> {row.arrival_soc_pct < 0 ? 'The EV is expected to run out of battery before arrival.'
                : `Estimated battery on arrival ${number(row.arrival_soc_pct, '%')}.`}
                {' '}Required reserve {number(row.reserve_pct, '%')}.</>}
            {compared
              ? <> {winner[0]} was chosen instead: it adds {minutesText(winner[1].gain_vehicle_minutes!)} of EV availability, compared with {minutesText(row.gain_vehicle_minutes!)} for this EV.</>
              : <>
                {winner && tiebreaks.includes(row.reason) && <> {winner[0]} was chosen instead.</>}
                {row.gain_vehicle_minutes != null && row.gain_vehicle_minutes > 0 && <> Would add {minutesText(row.gain_vehicle_minutes)} of EV availability.</>}
              </>}
            {row.origin_shortage_increase != null &&
              <> Its current zone would be short of a car for {minutesText(row.origin_shortage_increase)} more.</>}
            {row.dispatch_ids.map(dispatch => onRelocation &&
              <button key={dispatch} onClick={() => onRelocation(dispatch)}>View relocation for {id}</button>)}
          </li>;
        })}</ul>
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
    {result.decision !== 'DEFER' && <details>
      <summary>Technical details</summary>
      <p className="muted">CRDS checks {result.scenario_count} scenarios over {result.parameters.horizon_minutes} minutes and recommends moves only above {number(result.parameters.minimum_gain_vehicle_minutes, ' vehicle-minutes')} of worst-case shortage reduction.
        {(result.parameters.priority_coverage_pct ?? 0) > 0 &&
          <> Missing EVs count double up to the first {result.parameters.priority_coverage_pct}% of demand, rounded up to a whole EV.</>}
      </p>
      {Object.keys(result.shortage_changes_by_zone).length > 0 && <>
        <p className="muted">Unweighted shortage changes in the lowest-scoring scenario. Positive values show more shortage and negative values show less.</p>
        <dl className="candidate-metrics">{Object.entries(result.shortage_changes_by_zone).map(([zone, change]) =>
          <div key={zone}><dt>{zone}</dt><dd>{number(change, ' vehicle-minutes')}</dd></div>)}</dl>
      </>}
    </details>}
    <small className="identifier">Assessment reference {result.epoch_id}</small>
  </section>;
}
