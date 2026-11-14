const reasons: Readonly<Record<string, string>> = {
  ALREADY_IN_TARGET_ZONE: 'This EV is already in the destination zone and does not count toward incoming relocations.',
  DESTINATION_CAPACITY_LIMIT: 'The compatible destinations cannot accommodate additional EVs in this plan.',
  OPERATOR_REQUESTED_QUANTITY: 'Selected to fulfil the dispatcher’s requested quantity; review its modeled coverage impact.',
  USEFUL_SHORTAGE_REDUCTION: 'Moving this EV makes it less likely that customers in the destination zone find no car available, even in the busiest demand pattern CRDS tested.',
  NO_ELIGIBLE_RELOCATIONS: 'No EV was free, charged enough, and able to reach a parking or charging space in this zone.',
  NO_USEFUL_MOVE: 'None of the possible moves would help customers enough to be worth the trip.',
  NO_MORE_ELIGIBLE_EVS: 'No more eligible EVs remained for this request.',
  CAPACITY_OR_STOCK_LIMIT: 'The batch reached a parking, charging, or origin-zone supply limit.',
  BATCH_PARKING_LIMIT: 'Earlier moves in this plan used the remaining parking spaces at the destination.',
  BATCH_CHARGING_LIMIT: 'Earlier moves in this plan used the remaining charging spaces at the destination.',
  BATCH_ORIGIN_STOCK_LIMIT: 'Earlier moves left too few available EVs in the origin zone to support this move.',
  DONOR_SERVICE_FLOOR: 'This is the last available EV in a zone where customers need it, so it stays where it is.',
  REQUESTED_QUANTITY_REACHED: 'CRDS found the requested number of EVs before selecting this one.',
  LOWER_MODELED_BENEFIT: 'Another EV would help customers in the destination zone more.',
  HIGHER_ENERGY_TIEBREAK: 'Another EV would help customers just as much while using less battery.',
  STABLE_ORDER_TIEBREAK: 'Another EV would help just as much with the same battery use, and CRDS picks between equal options in a fixed order.',
  TELEMETRY_UNAVAILABLE: 'Current vehicle updates are unavailable.',
  NO_TELEMETRY: 'The EV has not reported its current position and battery level.',
  OFFLINE: 'The EV is offline.',
  STALE_TELEMETRY: 'The latest vehicle update is too old to verify availability.',
  VEHICLE_MISSING: 'This EV is missing from the latest fleet snapshot.',
  RENTED: 'The EV is currently rented.',
  CHARGING_TASK: 'The EV is assigned to a charging task.',
  CHARGING: 'The EV is charging or assigned to a charging task.',
  BUSY: 'The EV is already committed to a relocation.',
  UNAVAILABLE: 'Fleet reports that the EV is unavailable for relocation.',
  RELOCATION_FAILED: 'The EV has a failed relocation that still needs review.',
  RELOCATION_UNCONFIRMED: 'The previous relocation outcome is still unconfirmed.',
  RELOCATION_OWNERSHIP_UNCONFIRMED: 'Fleet and Dispatch could not confirm the EV’s current relocation.',
  DESTINATION_HOLD_UNCONFIRMED: 'An existing parking reservation at the destination is still unconfirmed.',
  NO_SUITABLE_DESTINATION: 'No suitable parking or charging destination was found in the target zone.',
  PARKING_CLOSED: 'The destination parking is closed.',
  FACILITY_DATA_STALE: 'Current parking or charging availability could not be verified.',
  PARKING_FULL: 'There are no available parking spaces at the destination.',
  CHARGING_FULL: 'This EV would need to charge on arrival, and no charging space is available.',
  CHARGING_ONLY_DESTINATION: 'The available site is for charging, and this EV is expected to arrive with enough battery to park elsewhere.',
  BELOW_OPERATIONAL_RESERVE: 'The estimated battery on arrival is below the required reserve.',
  ROUTE_BELOW_OPERATIONAL_RESERVE: 'The EV would arrive below the required battery reserve on the checked road route.',
  ROAD_ROUTE_BLOCKED: 'Traffic restrictions block the checked road route.',
  ROAD_ROUTE_NOT_FOUND: 'The routing service could not find a usable road route.',
  ROUTING_NOT_CONFIGURED: 'Road routing is not configured. Set ROUTING_PROVIDER=ONEMAP and ONEMAP_ACCESS_TOKEN in this computer’s .env, then restart the local deployment. No EV was dispatched.',
  ROUTING_ACCESS_UNAVAILABLE: 'OneMap routing credentials were rejected or are missing. Check or renew ONEMAP_ACCESS_TOKEN in .env, then restart the local deployment.',
  ROUTE_LOOKUP_TIMEOUT: 'Route verification did not finish in time. Try requesting a fresh recommendation.',
  PARTIAL_ROUTE_COVERAGE: 'Some routes could not be verified. The recommendation covers only EVs with completed route checks.',
  ROUTE_EVIDENCE_UNAVAILABLE: 'CRDS could not verify the road routes. Try requesting a fresh recommendation.',
  ROUTE_ORIGIN_CHANGED: 'The EV’s location changed before the route could be verified.',
  ROUTE_ESTIMATE_IDENTITY_CHANGED: 'The vehicle or its location changed during the route check.',
  CHARGING_AVAILABILITY_UNKNOWN: 'CRDS could not verify the charging needed after arrival.',
  STALE_AFTER_ROUTE_LOOKUP: 'Vehicle updates became too old while CRDS checked routes. Try again after the fleet updates.',
  STALE_AFTER_BATCH_PLANNING: 'Vehicle updates became too old while CRDS planned the batch. Try again after the fleet updates.',
  FLEET_SNAPSHOT_STALE: 'Fleet data is too old. Request a fresh recommendation after the fleet updates.',
  DISPATCH_SNAPSHOT_STALE: 'Current relocation records could not be verified. Try again after Dispatch updates.',
  DEMAND_UNKNOWN_OR_STALE: 'Current demand is unavailable. Request a fresh recommendation after demand data updates.',
  PARKING_ASSIGNMENT_UNCONFIRMED: 'Some parked EVs could not be assigned to a zone. Request a new assessment after parking data updates.',
  PARKING_FEED_UNAVAILABLE: 'Current parking availability is unavailable. Try again after parking data updates.',
  PENDING_DISPATCH_UNRESOLVED: 'Dispatch could not match a pending relocation to an EV. Check pending approvals.',
  DECISION_DEADLINE: 'The assessment did not finish in time. Try requesting a fresh recommendation.',
  WORKERS_UNAVAILABLE: 'The service that checks possible scenarios is unavailable. Try again shortly.',
  WORKER_RESULTS_INCOMPLETE: 'CRDS could not finish checking all scenarios. Try requesting a fresh recommendation.',
  ASSESSMENT_INCOMPLETE: 'CRDS could not complete this EV’s assessment.',
  INCOMPLETE_INPUTS: 'CRDS could not verify the information needed for this assessment.',
  TARGET_NOT_FOUND: 'The selected zone is unavailable. Select a zone again before requesting a recommendation.',
  CRDS_INPUT_TIMEOUT: 'Planning data did not arrive in time. Try requesting a fresh recommendation.',
  DEPENDENCY_TIMEOUT: 'Planning data did not arrive in time. Try requesting a fresh recommendation.',
  DEPENDENCY_UNAVAILABLE: 'A service needed for the assessment is unavailable. Try again shortly.',
  DEPENDENCY_INVALID_RESPONSE: 'CRDS could not verify the information returned by a planning service.',
  QUEUE_WAIT_EXCEEDED: 'The request waited too long to start. Request a fresh assessment from the map.',
  RECOMMENDATION_QUEUE_FULL: 'The recommendation queue is full. Try again when an assessment finishes.',
  RECOMMENDATION_QUEUE_DISABLED: 'The recommendation queue is disabled.',
  RECOMMENDATION_CAPACITY_REACHED: 'The recommendation service is busy. Try again shortly.',
};

// CRDS gain is EV-minutes of customer shortage avoided over the planning horizon.
export function minutesText(minutes: number) {
  if (minutes <= 0) return 'no time';
  if (minutes < 1) return 'less than a minute';
  const whole = Math.round(minutes);
  return `about ${whole} ${whole === 1 ? 'minute' : 'minutes'}`;
}

export function benefitPhrase(minutes: number, horizon: number, combined = false) {
  if (minutes <= 0) return 'no extra EV availability';
  return combined || minutes > horizon
    ? `the equivalent of one extra EV for ${minutesText(minutes)} over the next ${horizon} minutes`
    : `one extra EV available for ${minutesText(minutes)} of the next ${horizon} minutes`;
}

const exclusionLabels: Readonly<Record<string, string>> = {
  RENTED: 'rented', CHARGING: 'charging', CHARGING_TASK: 'charging', BUSY: 'already on another job',
  OFFLINE: 'offline', NO_TELEMETRY: 'not reporting', STALE_TELEMETRY: 'not reporting',
  TELEMETRY_UNAVAILABLE: 'not reporting', VEHICLE_MISSING: 'not reporting',
  BELOW_OPERATIONAL_RESERVE: 'battery too low', ROUTE_BELOW_OPERATIONAL_RESERVE: 'battery too low',
  PARKING_FULL: 'no space at the destination', CHARGING_FULL: 'no space at the destination',
  NO_SUITABLE_DESTINATION: 'no space at the destination', CHARGING_ONLY_DESTINATION: 'no space at the destination',
  ROAD_ROUTE_BLOCKED: 'no usable road route', ROAD_ROUTE_NOT_FOUND: 'no usable road route',
  RELOCATION_FAILED: 'previous move needs review', RELOCATION_UNCONFIRMED: 'previous move needs review',
  RELOCATION_OWNERSHIP_UNCONFIRMED: 'previous move needs review',
  DONOR_SERVICE_FLOOR: 'needed in its own zone',
};

export function exclusionSummary(reasons: string[]) {
  const counts = new Map<string, number>();
  for (const reason of reasons) {
    const label = Object.prototype.hasOwnProperty.call(exclusionLabels, reason) ? exclusionLabels[reason] : 'other reasons';
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts].sort((a, b) => b[1] - a[1]).map(([label, count]) => `${count} ${label}`).join(', ');
}

export function crdsMessage(code: string, fallback = 'CRDS could not verify the information needed for this assessment.') {
  return Object.prototype.hasOwnProperty.call(reasons, code) ? reasons[code] : fallback;
}

export function recommendationError(error: unknown) {
  return crdsMessage(error instanceof Error ? error.message : '',
    'The recommendation could not be loaded. Try requesting it again.');
}
