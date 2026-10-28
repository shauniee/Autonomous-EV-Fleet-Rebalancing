import type { TrafficSnapshot } from '../api/contracts';
import { time } from './format';

export function TrafficConditions({ snapshot, error }: {snapshot?: TrafficSnapshot; error?: string}) {
  const ready = snapshot?.source === 'LTA_DATAMALL';
  const issues = ready ? snapshot.incidents : [];
  const works = ready ? snapshot.roadworks : [];
  const date = new Date().toISOString().slice(0, 10);
  const active = works.filter(w => w.start_date <= date && date <= w.end_date);
  return <section className="panel" aria-label="Traffic conditions and roadworks">
    <div className="section-title"><h2>Traffic conditions & roadworks</h2>
      <strong>{error ? 'UNAVAILABLE' : snapshot?.status ?? 'LOADING'}</strong></div>
    {!ready && <p className="muted">Live LTA traffic ranking is disabled. Set TRAFFIC_SOURCE=LTA_DATAMALL and supply your backend-only AccountKey. No real-time congestion is assumed.</p>}
    {error && <p className="warning">Traffic service error: {error}. An unavailable feed must not be mistaken for clear roads.</p>}
    {ready && <>
      <p className="muted">LTA DataMall · snapshot {time(snapshot.generated_at)} · Traffic overlays approximate affected roads, not road closures or guaranteed legal access.</p>
      <div className="table-scroll"><table><thead><tr><th>Feed</th><th>Status</th><th>Observations</th><th>Last received</th></tr></thead>
      <tbody>{Object.entries(snapshot.feeds).map(([name, feed]) =>
        <tr key={name}><td>{name}</td><td>{feed.status}</td><td>{feed.count}</td><td>{time(feed.observed_at)}</td></tr>)}</tbody>
      </table></div>
      <h3>Geolocated incidents ({issues.length})</h3>
      {!issues.length && <p className="muted">No usable incident observations in the current feed. This is not proof that roads are clear.</p>}
      {issues.length > 0 && <div className="table-scroll"><table><thead><tr><th>Type</th><th>Message</th></tr></thead>
        <tbody>{issues.slice(0, 20).map(i => <tr key={i.id}><td>{i.type}</td><td>{i.message}</td></tr>)}</tbody></table></div>}
      <h3>Approved works active today ({active.length})</h3>
      {!active.length && <p className="muted">No active works in the available data. Roadworks do not automatically imply closures.</p>}
      {active.length > 0 && <div className="table-scroll"><table><thead><tr><th>Road</th><th>Until</th></tr></thead>
        <tbody>{active.slice(0, 20).map(w => <tr key={w.id}><td>{w.road}</td><td>{w.end_date}</td></tr>)}</tbody></table></div>}
      <p className="muted">Showing up to 20 incidents and 20 works. Route scores use available feed records; missing speed coverage remains uncertain. Live route changes require explicit approval.</p>
    </>}
  </section>;
}
