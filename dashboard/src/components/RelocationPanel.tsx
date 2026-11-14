import { useEffect, useState } from 'react';
import type { Dispatch, Facility, Vehicle, Zone } from '../api/contracts';
import { number, position, time } from './format';
import { approvalDetails, approvalLabels, approvalStatus, journeyIssue, journeyLabel } from './approvalMessages';
import type { Notify } from './Notifications';
import { OperationalTabs } from './OperationalTabs';
import { RecoveryControl } from './RecoveryControl';
import { RouteDetails } from './RouteDetails';

const pageSize = 6;
const localDateTime = new Intl.DateTimeFormat('en-SG', {
  timeZone: 'Asia/Singapore',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});
const initialFilters = { date: '', hour: '', outcome: '', vehicle: '', destination: '' };
const outcomes = [
  ['unfinished', 'Unfinished'],
  ['SUCCEEDED', 'Arrived'],
  ['FAILED', 'Failed'],
  ['REJECTED', 'Rejected'],
  ['ended', 'Ended by operator'],
];

function outcome(dispatch: Dispatch) {
  if (dispatch.failure_reason === 'OPERATOR_ENDED_RELOCATION') return 'ended';
  return ['SUCCEEDED', 'FAILED', 'REJECTED'].includes(dispatch.state) ? dispatch.state : 'unfinished';
}

function RelocationRoute({ dispatch, origin, destination }: { dispatch: Dispatch; origin: string; destination: string }) {
  const createdAt = new Date(dispatch.created_at);
  return <>
    {Number.isFinite(createdAt.getTime()) &&
      <div className="relocation-date muted">
        Requested <time dateTime={dispatch.created_at}>{localDateTime.format(createdAt)} SGT</time>
      </div>}
    <dl className="relocation-route">
      <div><dt>From</dt><dd>{origin}</dd></div>
      <div><dt>To</dt><dd>{destination}</dd></div>
    </dl>
  </>;
}

function needsRelocationResolution(dispatch: Dispatch, vehicle?: Vehicle) {
  if (dispatch.state === 'QUEUED') return false;
  return dispatch.state === 'UNKNOWN' || dispatch.state === 'FAILED'
    || dispatch.state === 'REJECTED' && (dispatch.claim_held !== false || dispatch.parking_status === 'RESERVED')
    || dispatch.claim_held !== false && (vehicle?.stale || vehicle?.connectivity === 'OFFLINE');
}
function RelocationIssue({ dispatch }: { dispatch: Dispatch }) {
  if (dispatch.state === 'QUEUED') return <p className="muted"><strong>Human approval accepted.</strong> Queued for admission. Starts and temporary retries run automatically; follow the batch progress in Dispatch.</p>;
  const status = approvalStatus(dispatch);
  const evidence = dispatch.revalidated_metrics?.approval_decision;
  const issue = status === 'APPROVED' ? journeyIssue(dispatch.state, dispatch.failure_reason) : '';
  return <>
    <p className={status === 'APPROVED' ? 'muted' : 'warning'}><strong>Approval {approvalLabels[status].toLowerCase()}.</strong>
      {' '}{approvalDetails(status, evidence, status === 'APPROVED' ? null : dispatch.failure_reason)}
    </p>
    {issue && <p className={dispatch.failure_reason === 'OPERATOR_ENDED_RELOCATION' ? 'muted' : 'warning'} role="status">{issue}</p>}
  </>;
}

function DispatchRow({ dispatch, vehicle, origin, destination, onTrack, notify }: {
  dispatch: Dispatch;
  vehicle?: Vehicle;
  origin: string;
  destination: string;
  onTrack: (id: string) => void;
  notify: Notify;
}) {
  const route = dispatch.command?.route;
  const ended = dispatch.failure_reason === 'OPERATOR_ENDED_RELOCATION';
  const showResolution = needsRelocationResolution(dispatch, vehicle) || ended;
  return <article id={`relocation-${dispatch.dispatch_id}`} tabIndex={-1} className="dispatch-card" data-testid={`dispatch-${dispatch.dispatch_id}`}>
    <div className="section-title"><h3>{dispatch.vehicle_id ?? 'Unassigned EV'}</h3>{approvalStatus(dispatch) === 'APPROVED' && <span>{ended ? 'Ended' : journeyLabel(dispatch.state)}</span>}</div>
    <RelocationIssue dispatch={dispatch}/>
    <RelocationRoute dispatch={dispatch} origin={origin} destination={destination}/>
    <dl className="candidate-metrics"><div><dt>Distance travelled</dt><dd>{number(dispatch.distance_travelled_km, ' km')}</dd></div>
      <div><dt>Initial estimate</dt><dd>{number(dispatch.revalidated_metrics?.distance_km, ' km')}</dd></div>
      <div><dt>{vehicle?.stale ? 'Last known battery' : 'Battery'}</dt><dd>{number(vehicle?.telemetry?.soc_pct, '%')}</dd></div>
      <div><dt>Move confirmed</dt><dd>{time(dispatch.acknowledged_at)}</dd></div>
      <div><dt>Last update</dt><dd>{time(dispatch.last_contact_at)}</dd></div>
      {dispatch.parking_facility_id && <div><dt>Destination hold</dt><dd>{dispatch.parking_status ?? 'Unconfirmed'}</dd></div>}
      {vehicle && <div><dt>Fleet availability</dt><dd>{vehicle.effective_availability}</dd></div>}
      <div><dt>Claim held</dt><dd>{dispatch.claim_held == null ? 'Unknown' : dispatch.claim_held ? 'Yes' : 'No'}</dd></div>
    </dl>
    {route && <><button className="map-reset" onClick={() => onTrack(dispatch.dispatch_id)}>View route on map</button>
      <details><summary>OneMap route & directions</summary><RouteDetails dispatch={dispatch} vehicle={vehicle}/></details></>}
    {(showResolution || dispatch.recovery) &&
      <RecoveryControl dispatchId={dispatch.dispatch_id} notify={notify} showResolution={showResolution}/>}
    <small className="identifier">{dispatch.dispatch_id}</small>
  </article>;
}
export function RelocationPanel({
  dispatches, vehicles, zones, facilities, focusId, onTrack, notify, approvalBusy, onCloseApproval, onRetryApproval,
}: {
  dispatches: Dispatch[];
  vehicles: Vehicle[];
  zones: Zone[];
  facilities: Facility[];
  focusId?: string;
  onTrack: (id: string) => void;
  notify: Notify;
  approvalBusy?: boolean;
  onCloseApproval?: (id: string) => void;
  onRetryApproval?: (dispatch: Dispatch) => void;
}) {
  const [search, setSearch] = useState(focusId ?? '');
  const [filters, setFilters] = useState(initialFilters);
  const [page, setPage] = useState(0);
  const vehicleById = new Map(vehicles.map(vehicle => [vehicle.vehicle_id, vehicle]));
  const zoneNames = new Map(zones.map(zone => [zone.zone_id, zone.name]));
  const facilityById = new Map(facilities.map(facility => [facility.facility_id, facility]));
  const vehicleIds = [...new Set(dispatches.map(dispatch => dispatch.vehicle_id ?? dispatch.revalidated_metrics?.approval_decision?.vehicle_id ?? ''))].filter(Boolean).sort();
  const destinationIds = [...new Set(dispatches.map(dispatch => dispatch.command?.target_zone_id ?? ''))]
    .filter(Boolean)
    .sort((a, b) => (zoneNames.get(a) ?? a).localeCompare(zoneNames.get(b) ?? b));
  const searchTerms = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const groups = ['Needs attention', 'Active', 'Recent outcomes'];

  const matches = [];
  for (const dispatch of dispatches) {
    const approval = dispatch.revalidated_metrics?.approval_decision;
    const vehicle_id = dispatch.vehicle_id ?? approval?.vehicle_id;
    const parking_facility_id = dispatch.parking_facility_id ?? approval?.facility_id;
    const recordedOrigin = dispatch.revalidated_metrics?.origin;
    const originZoneId = recordedOrigin?.zone_id ?? '';
    const originFacilityId = recordedOrigin?.facility_id ?? '';
    const vehicle = vehicleById.get(vehicle_id ?? '');
    const destinationId = dispatch.command?.target_zone_id ?? '';
    const result = outcome(dispatch);

    if (filters.outcome && filters.outcome !== result) continue;
    if (filters.vehicle && filters.vehicle !== vehicle_id) continue;
    if (filters.destination && filters.destination !== destinationId) continue;

    const createdAt = new Date(dispatch.created_at);
    const timestamp = createdAt.getTime();
    if (filters.date || filters.hour) {
      if (!Number.isFinite(timestamp)) continue;
      const parts = Object.fromEntries(localDateTime.formatToParts(createdAt).map(part => [part.type, part.value]));
      if (filters.date && filters.date !== `${parts.year}-${parts.month}-${parts.day}`) continue;
      if (filters.hour && Number(filters.hour) !== Math.floor(Number(parts.hour) / 6) * 6) continue;
    }

    const originZone = zoneNames.get(originZoneId) ?? originZoneId;
    const destinationZone = zoneNames.get(destinationId) ?? destinationId;
    const originFacility = facilityById.get(originFacilityId);
    const destinationFacility = facilityById.get(parking_facility_id ?? '');
    const originPosition = recordedOrigin?.position ?? dispatch.command?.route?.geometry[0];
    const origin = originFacility?.name || originFacilityId || originZone
      || (originPosition ? position(originPosition) : 'Unknown starting point');
    const destination = destinationFacility?.name ?? parking_facility_id ?? (destinationZone || 'Unknown destination');
    if (searchTerms.length) {
      const searchable = [
        vehicle_id, dispatch.dispatch_id, origin, destination, originZone, originZoneId, destinationZone,
        destinationId, originFacilityId, parking_facility_id, originFacility?.address, destinationFacility?.address,
        originPosition && position(originPosition), dispatch.command && position(dispatch.command.target_position),
      ].filter(Boolean).join(' ').toLowerCase();
      if (!searchTerms.every(term => searchable.includes(term))) continue;
    }

    const ended = result === 'ended';
    const needsAttention = needsRelocationResolution(dispatch, vehicle)
      || ended && !!parking_facility_id && dispatch.parking_status !== 'RELEASED'
      || !ended && ['BLOCKED', 'REQUESTED', 'RESTORING'].includes(dispatch.recovery?.status ?? '');
    const group = needsAttention ? 0 : result === 'unfinished' ? 1 : 2;
    matches.push({ dispatch, vehicle, origin, destination, group, timestamp });
  }
  matches.sort((a, b) => a.group - b.group || (b.timestamp || 0) - (a.timestamp || 0));

  const pageCount = Math.max(1, Math.ceil(matches.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const start = currentPage * pageSize;
  const visible = matches.slice(start, start + pageSize);
  const focusVisible = !!focusId && visible.some(({ dispatch }) => dispatch.dispatch_id === focusId);
  useEffect(() => {
    if (!focusVisible) return;
    const frame = requestAnimationFrame(() => {
      const card = document.getElementById(`relocation-${focusId}`);
      card?.focus({ preventScroll: true });
      card?.scrollIntoView({ block: 'nearest' });
    });
    return () => cancelAnimationFrame(frame);
  }, [focusId, focusVisible]);

  function setFilter(name: keyof typeof initialFilters, value: string) {
    setFilters(current => ({ ...current, [name]: value }));
    setPage(0);
  }

  function filterSelect(name: keyof typeof initialFilters, label: string, all: string, options: string[][]) {
    return <label className="relocation-field">
      {label}
      <select value={filters[name]} onChange={event => setFilter(name, event.target.value)}>
        <option value="">{all}</option>
        {options.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
      </select>
    </label>;
  }

  const hasFilters = search.length > 0 || Object.values(filters).some(Boolean);
  return <section className="panel relocation-view">
    <div className="section-title">
      <h2>Relocations</h2>
      <span className="muted">Live moves and past outcomes</span>
    </div>
    <div className="relocation-tools">
      <label className="relocation-field relocation-search">
        Search relocations
        <input type="search" value={search} placeholder="EV, starting point or destination"
          onChange={event => { setSearch(event.target.value); setPage(0); }}/>
      </label>
      <OperationalTabs label="Filter relocations by" tabs={[
        {
          id: 'date', label: 'Date', summary: filters.date ? '•' : undefined,
          content: <label className="relocation-field">
            Request date (SGT)
            <input type="date" value={filters.date} onChange={event => setFilter('date', event.target.value)}/>
          </label>,
        },
        {
          id: 'time', label: 'Time', summary: filters.hour ? '•' : undefined,
          content: filterSelect('hour', 'Request time (SGT)', 'Any time', [
            ['0', '00:00–05:59'], ['6', '06:00–11:59'], ['12', '12:00–17:59'], ['18', '18:00–23:59'],
          ]),
        },
        {
          id: 'outcome', label: 'Outcome', summary: filters.outcome ? '•' : undefined,
          content: filterSelect('outcome', 'Relocation outcome', 'All outcomes', outcomes),
        },
        {
          id: 'vehicle', label: 'EV', summary: filters.vehicle ? '•' : undefined,
          content: filterSelect('vehicle', 'EV name', 'All EVs', vehicleIds.map(id => [id, id])),
        },
        {
          id: 'destination', label: 'Destination', summary: filters.destination ? '•' : undefined,
          content: filterSelect('destination', 'Destination zone', 'All destination zones',
            destinationIds.map(id => [id, zoneNames.get(id) ?? id])),
        },
      ]}/>
    </div>
    <div className="relocation-results">
      <span role="status" className="muted">
        {matches.length ? `Showing ${start + 1}–${start + visible.length} of ${matches.length}` : '0 relocations'}
      </span>
      {hasFilters && <button className="text-button" onClick={() => {
        setSearch('');
        setFilters(initialFilters);
        setPage(0);
      }}>Clear search and filters</button>}
      {pageCount > 1 && <nav aria-label="Relocation pages">
        <button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button>
        <span className="muted">Page {currentPage + 1} of {pageCount}</span>
        <button disabled={currentPage === pageCount - 1} onClick={() => setPage(currentPage + 1)}>Next</button>
      </nav>}
    </div>
    {!matches.length && <p className="empty">{dispatches.length
      ? 'No relocations match. Try a different search or clear the filters.'
      : 'No relocations to show yet.'}</p>}
    <div className="dispatch-grid relocation-groups">
      {groups.flatMap((title, group) => {
        const entries = visible.filter(entry => entry.group === group);
        if (!entries.length) return [];
        return [
          <h3 key={title} className="relocation-group-title">{title}</h3>,
          ...entries.map(({ dispatch, vehicle, origin, destination }) => dispatch.vehicle_id
            ? <DispatchRow key={dispatch.dispatch_id} dispatch={dispatch} vehicle={vehicle}
                origin={origin} destination={destination} onTrack={onTrack} notify={notify}/>
            : <article key={dispatch.dispatch_id} id={`relocation-${dispatch.dispatch_id}`}
                className="dispatch-card" tabIndex={-1} data-testid={`dispatch-${dispatch.dispatch_id}`}>
                <div className="section-title"><h3>{dispatch.revalidated_metrics?.approval_decision?.vehicle_id ?? 'Unassigned EV'}</h3></div>
                <RelocationIssue dispatch={dispatch}/>
                <RelocationRoute dispatch={dispatch} origin={origin} destination={destination}/>
                {dispatch.state === 'UNKNOWN' && dispatch.claim_held === false && !dispatch.command &&
                  <div className="actions">
                    {dispatch.recommendation_id && <button disabled={approvalBusy}
                      onClick={() => onRetryApproval?.(dispatch)}>Retry same approval</button>}
                    <button disabled={approvalBusy}
                      onClick={() => onCloseApproval?.(dispatch.request_id)}>Close unstarted approval</button>
                  </div>}
                <small className="identifier">{dispatch.dispatch_id}</small>
              </article>),
        ];
      })}
    </div>
  </section>;
}
