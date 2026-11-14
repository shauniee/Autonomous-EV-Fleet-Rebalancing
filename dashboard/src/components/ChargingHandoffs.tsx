import { useEffect, useRef, useState } from 'react';
import type { Vehicle, VehicleParking } from '../api/contracts';
import { api, ApiError } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import type { Notify } from './Notifications';
import { number } from './format';

type CableAction = 'CONNECT' | 'DISCONNECT';
interface CableRequest { dispatch_id: string; request_id: string; action: CableAction; staff_name: string }
const errors: Record<string, string> = {
  CHARGING_SESSION_CHANGED: 'The charging visit changed. Refresh the bay observations.',
  CHARGING_ALLOCATION_NOT_READY: 'Wait for arrival and dispatch completion before handling the cable.',
  CHARGING_CABLE_STATE_CHANGED: 'The cable stage has changed. Refresh to see the next action.',
  CHARGING_FRESH_STOPPED_EV_REQUIRED: 'Fresh stopped-vehicle telemetry is required. Wait for the vehicle update and retry.',
  CHARGING_FLEET_EVIDENCE_UNAVAILABLE: 'Fleet could not verify the vehicle. Restore the connection and retry.',
  CHARGING_HANDOFF_UNAVAILABLE: 'The handoff service is unavailable. Restore the connection and retry.',
};

export function ChargingHandoffs({ vehicles, fleetFresh, onUpdated, notify }: {
  vehicles: Vehicle[]; fleetFresh: boolean; onUpdated: () => void; notify: Notify;
}) {
  const bays = usePolling<Record<string, VehicleParking>>('/api/facilities/vehicle-parking');
  const [team, setTeam] = useState('Ground Team A');
  const [busy, setBusy] = useState('');
  const [feedback, setFeedback] = useState<{ id: string; session: string; message: string; error: boolean }>();
  const pending = useRef(new Map<string, CableRequest>());
  const submitting = useRef(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const valid = !!bays.data && !Array.isArray(bays.data) && Object.values(bays.data).every(value =>
    value && typeof value.vehicle_id === 'string' && typeof value.pending === 'boolean'
    && (value.parking === null || (value.parking && typeof value.parking.facility_id === 'string')));
  const fresh = valid && !bays.error && !!bays.updatedAt && now - Date.parse(bays.updatedAt) < 10000;
  const visits = valid ? Object.values(bays.data!).filter(value => value.parking?.charging_allocated)
    .sort((a, b) => a.vehicle_id.localeCompare(b.vehicle_id)) : [];

  useEffect(() => {
    if (!fresh || !feedback?.error) return;
    const visit = bays.data?.[feedback.id];
    if (!visit?.parking) return;
    const site = visit.parking;
    if (site.charging_session_id !== feedback.session) {
      setFeedback({ ...feedback, error: false, message: 'The charging visit changed. Review the current cable stage before continuing.' });
      return;
    }
    for (const action of ['CONNECT', 'DISCONNECT'] as const) {
      const key = `${feedback.session}:${action}`;
      if (!pending.current.has(key)) continue;
      const confirmed = action === 'CONNECT'
        ? site.cable_status === 'CONNECTED' || site.cable_status === 'DISCONNECTED'
        : site.cable_status === 'DISCONNECTED';
      if (confirmed) {
        pending.current.delete(key);
        setFeedback({ ...feedback, error: false, message: action === 'CONNECT'
          ? 'Cable connection confirmed by charging-bay observations.'
          : 'Cable disconnection confirmed by charging-bay observations.' });
        break;
      }
    }
  }, [bays.data, feedback, fresh]);

  async function confirm(visit: VehicleParking, action: CableAction) {
    const session = visit.parking?.charging_session_id;
    if (!session || submitting.current) return;
    submitting.current = true;
    setBusy(visit.vehicle_id);
    setFeedback(undefined);
    const key = `${session}:${action}`;
    const body = pending.current.get(key) ?? {
      dispatch_id: session, request_id: crypto.randomUUID(), action, staff_name: team.trim(),
    };
    pending.current.set(key, body);
    try {
      await api(`/api/facilities/vehicles/${encodeURIComponent(visit.vehicle_id)}/charging-cable`, {
        method: 'POST', body: JSON.stringify(body),
      });
      pending.current.delete(key);
      const message = action === 'CONNECT'
        ? 'Cable connection recorded. Waiting for the simulator to report charging.'
        : 'Cable disconnection recorded. Waiting for fresh vehicle availability.';
      setFeedback({ id: visit.vehicle_id, session, message, error: false });
      notify('info', `${visit.vehicle_id}: ${message}`);
      bays.refresh();
      onUpdated();
    } catch (error) {
      const detail = error instanceof ApiError ? errors[error.message] : undefined;
      setFeedback({ id: visit.vehicle_id, session, error: true,
        message: `${detail ?? 'The cable confirmation could not be verified.'} Retry this confirmation to check the same request.` });
      // Retain the complete request, including its team, after an uncertain
      // response. Retrying cannot create another physical transition.
    } finally {
      setBusy('');
      submitting.current = false;
    }
  }

  return <section className="charging-handoffs" aria-label="Ground-team charging handoff">
    <h3>Ground-team charging handoff</h3>
    <p className="muted">An approved EV travels to the charger. A person connects the cable; the simulator charges to its target. A person disconnects the cable before the EV can return to service.</p>
    <ol className="charging-handoff-flow" aria-label="Charging sequence">
      <li>Approved arrival</li><li>Connect cable</li><li>Simulated charging</li><li>Disconnect &amp; release</li>
    </ol>
    <label className="charging-team">Ground team for cable handoff
      <input value={team} maxLength={64} disabled={!!busy} onChange={event => setTeam(event.target.value)} />
    </label>
    <p className="muted charging-handoff-note">These confirmations record simulated physical work. They do not operate a real charger.</p>
    {(!valid || bays.error || !fresh) && <p className="warning" role="status">
      {bays.error || (bays.data && !valid) || (valid && !fresh)
        ? 'Charging-bay observations unavailable or stale. Do not assume an empty queue.'
        : 'Loading occupied charging bays…'}{' '}
      <button type="button" onClick={bays.refresh}>Refresh charging bays</button>
    </p>}
    {fresh && !visits.length && <p className="muted">No EVs occupying allocated charging bays. Review charger advice below to prepare a human-approved relocation.</p>}
    <div className="charging-visit-list">{visits.map(visit => {
      const site = visit.parking!;
      const vehicle = vehicles.find(item => item.vehicle_id === visit.vehicle_id);
      const telemetry = vehicle?.telemetry;
      const age = telemetry ? now - Date.parse(telemetry.observed_at) : Infinity;
      const vehicleFresh = fleetFresh && vehicle && !vehicle.stale && vehicle.connectivity === 'ONLINE'
        && age >= -5000 && age < 15000;
      const stopped = vehicleFresh && telemetry?.parked_facility_id === site.facility_id
        && telemetry.speed_kph === 0 && vehicle.effective_relocation_state === 'IDLE'
        && !telemetry.active_dispatch_id && !vehicle.active_claim && !vehicle.active_rental && !vehicle.active_charging_task;
      const waiting = site.cable_status === 'AWAITING_CONNECTION';
      const connected = site.cable_status === 'CONNECTED';
      const complete = connected && telemetry?.charging_state === 'COMPLETE'
        && telemetry.soc_pct >= site.charging_target_soc_pct;
      const released = site.cable_status === 'DISCONNECTED';
      const stage = visit.pending ? 'Relocation pending' : waiting ? 'Awaiting cable connection'
        : complete && vehicleFresh ? 'Awaiting cable disconnection'
        : connected && vehicleFresh && telemetry?.charging_state === 'CHARGING' ? 'Charging in progress'
        : connected ? 'Waiting for vehicle confirmation'
        : released && vehicleFresh && telemetry?.charging_state === 'NOT_CHARGING'
          && vehicle?.effective_availability === 'AVAILABLE' ? 'Ready for service' : 'Waiting for vehicle confirmation';
      const action: CableAction | null = waiting ? 'CONNECT' : complete ? 'DISCONNECT' : null;
      const enabled = fresh && stopped && !visit.pending && !!site.charging_session_id && !!team.trim()
        && (action === 'DISCONNECT' || telemetry?.charging_state === 'AWAITING_CONNECTION');
      return <article className="charging-visit" key={`${visit.vehicle_id}:${site.charging_session_id}`} aria-label={`Charging handoff for ${visit.vehicle_id}`}>
        <div className="charging-visit-identity"><strong>{visit.vehicle_id}</strong><span>{site.name}</span>
          <small className="muted">{vehicleFresh ? number(telemetry?.soc_pct, '% SOC') : 'SOC unavailable'} · Target {number(site.charging_target_soc_pct, '%')}</small>
        </div>
        <div className="charging-visit-state"><strong>{stage}</strong>
          <span className="muted">{released ? 'Availability follows fresh EV telemetry.' : 'Unavailable for rentals and relocation.'}</span>
          {site.cable_connected_by && <small className="muted">Connected by {site.cable_connected_by}</small>}
          {site.cable_disconnected_by && <small className="muted">Disconnected by {site.cable_disconnected_by}</small>}
        </div>
        <div className="charging-visit-action">
          {action && <button type="button" className="primary" disabled={!enabled || !!busy}
            aria-label={`Confirm cable ${action === 'CONNECT' ? 'connected' : 'disconnected'} for ${visit.vehicle_id}`}
            onClick={() => void confirm(visit, action)}>
            {busy === visit.vehicle_id ? 'Recording…' : action === 'CONNECT' ? 'Confirm cable connected' : 'Confirm cable disconnected'}
          </button>}
          {action && !stopped && <small className="muted">Fresh stopped-vehicle telemetry is required. Wait for the next update.</small>}
          {action && !team.trim() && <small className="muted">Enter the ground team who handled the cable.</small>}
          {feedback?.id === visit.vehicle_id && <p className={feedback.error ? 'warning' : 'charging-confirmation'} role={feedback.error ? 'alert' : 'status'}>{feedback.message}</p>}
        </div>
      </article>;
    })}</div>
  </section>;
}
