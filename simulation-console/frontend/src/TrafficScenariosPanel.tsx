import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

type Coordinate = { latitude: number; longitude: number };
export type RouteDispatch = { dispatch_id: string; vehicle_id: string | null; state: string;
  command: { route?: { source: string; geometry: Coordinate[] } | null } | null };
type Scenario = { id: string; kind: 'CONGESTION' | 'ROADWORKS' | 'ROAD_CLOSED';
  dispatch_id: string; segment_index: number; speed_kph: number | null;
  expires_at: string; start: Coordinate; end: Coordinate };
type Snapshot = { version: number; enabled: boolean; scenarios: Scenario[] };

export function TrafficScenariosPanel({ dispatches, available }: {
  dispatches: RouteDispatch[]; available: boolean;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [selected, setSelected] = useState('');
  const [segment, setSegment] = useState('0');
  const [kind, setKind] = useState<Scenario['kind']>('CONGESTION');
  const [speed, setSpeed] = useState('15');
  const [minutes, setMinutes] = useState('30');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const candidates = dispatches.filter(d => d.command?.route?.geometry &&
    d.command.route.geometry.length > 1 && !['FAILED','REJECTED'].includes(d.state));
  const chosen = candidates.find(d => d.dispatch_id === selected);
  const route = chosen?.command?.route?.geometry;
  const maxIndex = Math.max(0, (route?.length ?? 2) - 2);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetch('/api/probe/v1/traffic/scenarios', { signal: AbortSignal.timeout(5000) });
        if (!response.ok) throw new Error('Traffic controller unavailable');
        const value: Snapshot = await response.json();
        if (!stopped) { setSnapshot(value); setError(''); }
      } catch {
        if (!stopped) setError('Traffic controller unavailable; scenario state may be stale.');
      }
      if (!stopped) timer = setTimeout(poll, 5000);
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!available || !snapshot?.enabled || !route || busy) return;
    const index = Number(segment), speedValue = Number(speed), duration = Number(minutes);
    if (!Number.isInteger(index) || index < 0 || index > maxIndex ||
        !Number.isInteger(duration) || duration < 1 || duration > 120 ||
        (kind !== 'ROAD_CLOSED' && (!Number.isFinite(speedValue) || speedValue < 3 || speedValue > 80))) {
      setError('Select a valid approved route segment, 3–80 km/h speed and 1–120 minutes.'); return;
    }
    await write('POST', '/api/probe/v1/traffic/scenarios', {
      dispatch_id: selected, segment_index: index, kind, expected_version: snapshot.version,
      speed_kph: kind === 'ROAD_CLOSED' ? null : speedValue,
      duration_minutes: duration,
    });
  }

  async function write(method: string, url: string, body?: object) {
    if (!snapshot || busy) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const response = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(8000) });
      const result = await response.json();
      if (!response.ok) {
        setError(result.detail?.message || 'Scenario operation rejected. Refresh to check the current version.');
        return;
      }
      setSnapshot(result as Snapshot);
      setMessage('Traffic scenario updated. Operator Dashboard refreshes from Rebalancing.');
    } catch {
      setError('Operation outcome uncertain. Check the latest scenario list before trying again.');
    } finally { setBusy(false); }
  }
  const current = snapshot?.scenarios ?? [];
  return <section aria-label="Traffic simulation controller">
    <h2>Traffic scenarios</h2>
    <div className="section-toolbar">
      <p role="status"><strong>{snapshot?.enabled ? 'Scenario control enabled' : 'Scenario control disabled'}</strong>
        {' · '}{current.length} active · version {snapshot?.version ?? 'Unknown'}</p>
      <button disabled={busy || !current.length} onClick={() => write('DELETE',
        `/api/probe/v1/traffic/scenarios?expected_version=${snapshot!.version}`)}>Clear all scenarios</button>
    </div>
    <div className="split-layout">
      <div>
        <form className="traffic-form" onSubmit={submit}>
          <div className="traffic-route">
            <label>Approved route
              <select required value={selected} disabled={busy || !available || !snapshot?.enabled}
                onChange={e => { setSelected(e.target.value); setSegment('0'); }}>
                <option value="">Choose an EV journey</option>
                {candidates.map(d => <option key={d.dispatch_id} value={d.dispatch_id}>
                  {d.vehicle_id} · {d.state} · {d.dispatch_id.slice(0,8)} ({d.command!.route!.geometry.length - 1} segments)
                </option>)}
              </select>
            </label>
            <label>Segment (0–{maxIndex})
              <input type="number" required min="0" max={maxIndex} step="1"
                disabled={!route || busy} value={segment} onChange={e => setSegment(e.target.value)}/>
            </label>
          </div>
          {route && <p className="description">Selected road: {route[Number(segment)]?.latitude.toFixed(5) ?? '—'},
            {route[Number(segment)]?.longitude.toFixed(5) ?? '—'} to{' '}
            {route[Number(segment)+1]?.latitude.toFixed(5) ?? '—'},
            {route[Number(segment)+1]?.longitude.toFixed(5) ?? '—'}.</p>}
          <div className="traffic-fields">
            <label>Condition
              <select value={kind} onChange={e => setKind(e.target.value as Scenario['kind'])} disabled={busy}>
                <option value="CONGESTION">Heavy congestion (orange)</option>
                <option value="ROADWORKS">Roadworks (amber)</option>
                <option value="ROAD_CLOSED">Road closed (red)</option>
              </select>
            </label>
            {kind !== 'ROAD_CLOSED' && <label>Simulated speed (km/h)
              <input type="number" value={speed} min="3" max="80" required
                onChange={e => setSpeed(e.target.value)} disabled={busy}/>
            </label>}
            <label>Expires after (minutes)
              <input type="number" value={minutes} min="1" max="120" required
                onChange={e => setMinutes(e.target.value)} disabled={busy}/>
            </label>
          </div>
          <button className="primary-action" type="submit" disabled={!snapshot?.enabled || !available || !route || busy}>
            {busy ? 'Applying…' : 'Apply simulated condition'}</button>
        </form>
        {error && <p role="alert" className="warning">{error}</p>}
        {message && <p role="status" className="success">{message}</p>}
      </div>
      <div>
        <h3>Active road conditions</h3>
        <div className="table-wrap"><table><thead><tr>
          <th>Condition</th><th>Road segment</th><th>Speed</th><th>Expires</th><th>Action</th>
        </tr></thead><tbody>{current.map(s => <tr key={s.id}>
          <td>{s.kind}</td><td>{s.dispatch_id.slice(0,8)} · segment {s.segment_index}</td>
          <td>{s.speed_kph == null ? 'Closed' : s.speed_kph + ' km/h'}</td>
          <td>{new Date(s.expires_at).toLocaleTimeString('en-SG', {timeZone:'Asia/Singapore'})} SGT</td>
          <td><button disabled={busy} onClick={() => write('DELETE',
            `/api/probe/v1/traffic/scenarios/${s.id}?expected_version=${snapshot!.version}`)}>
            Remove</button></td>
        </tr>)}</tbody></table></div>
        {!current.length && <p className="description">No simulated road conditions active.</p>}
      </div>
    </div>
    <details className="help">
      <summary>Traffic scenario guidance</summary>
      <p className="description">Choose a segment you can identify on the Operator Dashboard from a real
        approved road route. Scenarios can affect traffic-aware recommendations and dispatch checks.
        They never authorise EV movement or issue a new command.</p>
      <p className="description">Mode resets on Rebalancing service restart. Missing live traffic is never
        considered proof that roads are clear. The simulation console is a privileged local-only interface.</p>
    </details>
  </section>;
}
