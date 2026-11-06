import { useEffect, useState } from 'react';
import type { Zone } from './DemandForm';

export type RentalVehicle = {
  vehicle_id: string;
  current_zone_id: string | null;
  connectivity: string;
  stale: boolean;
  effective_availability: string;
  telemetry: { soc_pct?: number; position?: { latitude: number; longitude: number } } | null;
  battery_capacity_kwh?: number;
  consumption_kwh_per_km?: number;
  active_rental?: {
    rental_id: string; expected_end_at: string; expected_return_zone_id?: string | null;
    status: string;
  } | null;
};

type ForecastZone = Zone & { position?: { latitude: number; longitude: number } | null };
type Status = { running: boolean; desired_replicas: number; actual_replicas: number };

function km(a: { latitude: number; longitude: number }, b: { latitude: number; longitude: number }) {
  const rad = Math.PI / 180, lat = (b.latitude - a.latitude) * rad, lng = (b.longitude - a.longitude) * rad;
  const h = Math.sin(lat / 2) ** 2 + Math.cos(a.latitude * rad) * Math.cos(b.latitude * rad) * Math.sin(lng / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.min(1, Math.sqrt(h)));
}

export function RentalPanel({ vehicles, zones, available }: {
  vehicles: RentalVehicle[]; zones: ForecastZone[]; available: boolean;
}) {
  const [view, setView] = useState<'active' | 'forecast'>('active');
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetch('/api/probe/v1/rentals/status', { signal: AbortSignal.timeout(5000) });
        if (!response.ok) throw new Error('Rental controller is unavailable');
        const next: Status = await response.json();
        if (!stopped) { setStatus(next); setError(''); }
      } catch (e) {
        if (!stopped) { setError(e instanceof Error ? e.message : 'Controller unavailable'); setStatus(null); }
      }
      if (!stopped) timer = setTimeout(poll, 5000);
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [busy]);

  async function control(action: 'start' | 'stop') {
    setBusy(true); setNotice('');
    try {
      const response = await fetch(`/api/probe/v1/rentals/${action}`, {
        method: 'POST', signal: AbortSignal.timeout(8000),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? `HTTP ${response.status}`);
      }
      const next: Status = await response.json();
      setStatus(next);
      setNotice(action === 'start'
        ? 'Rental simulator is starting. Wait for its six vehicles to register.'
        : 'Rental simulator is stopping. Existing Fleet records remain visible.');
    } catch (e) { setNotice(`Action rejected: ${e instanceof Error ? e.message : 'Unknown error'}`); }
    finally { setBusy(false); }
  }

  const now = Date.now(), horizon = now + 30 * 60_000;
  const rentals = vehicles.filter(v => !!v.active_rental);
  const forecast = zones.map(zone => {
    const availableNow = vehicles.filter(v =>
      v.current_zone_id === zone.zone_id && v.effective_availability === 'AVAILABLE'
      && v.connectivity === 'ONLINE' && !v.stale).length;
    const expected = rentals.filter(v => {
      const r = v.active_rental!;
      const eta = Date.parse(r.expected_end_at);
      if (v.stale || v.connectivity !== 'ONLINE' || r.expected_return_zone_id !== zone.zone_id
          || !Number.isFinite(eta) || eta < now || eta > horizon
          || !zone.position || !v.telemetry?.position || typeof v.telemetry.soc_pct !== 'number'
          || !v.battery_capacity_kwh || !v.consumption_kwh_per_km) return false;
      const remainingSoc = v.telemetry.soc_pct - 100 * km(v.telemetry.position, zone.position)
        * 1.3 * 1.15 * v.consumption_kwh_per_km / v.battery_capacity_kwh;
      return remainingSoc >= 30;
    }).length;
    const desired = zone.demand?.desired_available_vehicles;
    return { zone, availableNow, expected,
      shortage: desired == null ? null : Math.max(0, desired - availableNow),
      possibleShortage: desired == null ? null : Math.max(0, desired - availableNow - expected),
    };
  });

  return <section>
    <h2>Customer rentals & anticipated returns</h2>
    <p className="description">The optional six-car rental demo runs in this Kubernetes cluster. Customer journey destinations are synthetic. Rentals always remain unavailable for relocation until the actual return is confirmed by Fleet.</p>
    <p role="status">Rental workload: <strong>{error ? 'Unavailable' : status?.running ? 'Enabled' : status ? 'Paused' : 'Checking…'}</strong>
      {status && ` · ${status.actual_replicas}/${status.desired_replicas} existing Pods`}</p>
    {error && <p className="warning">{error}</p>}
    <div className="button-row">
      <button className="primary-action" disabled={busy || !status || status.running} onClick={() => void control('start')}>Start customer rentals</button>
      <button disabled={busy || !status || !status.running} onClick={() => void control('stop')}>Stop after active rentals finish</button>
    </div>
    {notice && <p role="status">{notice}</p>}
    <div className="view-options" role="group" aria-label="Rental observations">
      <button type="button" aria-pressed={view === 'active'} onClick={() => setView('active')}>Active rentals ({rentals.length})</button>
      <button type="button" aria-pressed={view === 'forecast'} onClick={() => setView('forecast')}>Return forecast</button>
    </div>
    <div hidden={view !== 'active'}>
      <h3>Active customer rentals ({rentals.length})</h3>
      <div className="table-wrap"><table><thead><tr><th>Vehicle</th><th>Anticipated return zone</th><th>Expected return</th><th>Current SOC</th></tr></thead>
        <tbody>{rentals.map(v => <tr key={v.vehicle_id}><td>{v.vehicle_id}</td>
          <td>{v.active_rental?.expected_return_zone_id ?? 'Unknown'}</td>
          <td>{v.active_rental?.expected_end_at ? new Date(v.active_rental.expected_end_at).toLocaleTimeString('en-SG', { timeZone: 'Asia/Singapore' }) + ' SGT' : 'Unknown'}</td>
          <td>{typeof v.telemetry?.soc_pct === 'number' ? v.telemetry.soc_pct.toFixed(1) + '%' : 'Unknown'}</td></tr>)}</tbody></table></div>
      {!rentals.length && <p>No active rentals observed.</p>}
    </div>
    <div hidden={view !== 'forecast'}>
      <h3>Possible available supply in the next 30 minutes</h3>
      <p className="description">Advisory only: the table assumes no additional pickups, uses reported zone assignment and estimates return energy from straight-line distance with safety factors. It is <strong>not</strong> an automatic rebalancing input or a guarantee of a return.</p>
      <div className="table-wrap"><table><thead><tr><th>Zone</th><th>Available now</th><th>Potential rental returns</th><th>Shortage now</th><th>Shortage if returns arrive</th></tr></thead>
        <tbody>{forecast.map(f => <tr key={f.zone.zone_id}><td>{f.zone.name}</td><td>{available ? f.availableNow : 'Unknown'}</td>
          <td>{available ? f.expected : 'Unknown'}</td><td>{available ? f.shortage ?? 'Unknown' : 'Unknown'}</td>
          <td>{available ? f.possibleShortage ?? 'Unknown' : 'Unknown'}</td></tr>)}</tbody></table></div>
      {!zones.length && <p>No zone demand observations available.</p>}
    </div>
  </section>;
}
