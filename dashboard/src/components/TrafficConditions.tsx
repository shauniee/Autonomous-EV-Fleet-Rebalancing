import type { TrafficSnapshot } from '../api/contracts';
import { time } from './format';

export function TrafficConditions({ snapshot, error }: {snapshot?: TrafficSnapshot; error?: string}) {
  const live = snapshot?.source === 'LTA_DATAMALL';
  const simulated = snapshot?.source === 'SIMULATION';
  const issues = live ? snapshot.incidents : [];
  const works = live ? snapshot.roadworks : [];
  const today = new Date().toLocaleDateString('en-CA', {timeZone:'Asia/Singapore'});
  const active = works.filter(w => w.start_date <= today && today <= w.end_date);
  return <section className="panel" aria-label="Traffic conditions and roadworks">
    <div className="section-title"><h2>Traffic conditions & roadworks</h2>
      <strong>{error ? 'UNAVAILABLE' : simulated ? 'SIMULATION MODE' : live ? 'LIVE LTA MODE' : 'DISABLED'}</strong></div>
    {error && <p className="warning" role="status">Traffic feed unavailable: {error}. Cached road conditions must not be interpreted as a clear route.</p>}
    <div className="map-marker-help" aria-label="Traffic map legend">
      <strong>Map legend:</strong> <span>🟢 Normal / higher observed speed</span>{' · '}
      <span>🟠 Congestion</span>{' · '}<span>🟡 Roadworks</span>{' · '}
      <span>🔴 Explicit road closure</span>
    </div>
    {simulated && <>
      <p className="muted">Simulation only · generated {time(snapshot.generated_at)} ·
        {snapshot.simulated_events?.length ?? 0} active scenarios. The route map uses the exact approved road segments
        selected in the Simulation Console. Live LTA readings are not mixed with fictional events.</p>
      <div className="table-scroll"><table aria-label="Simulated traffic scenarios">
        <thead><tr><th>Condition</th><th>Route / segment</th><th>Simulated speed</th><th>Expires</th></tr></thead>
        <tbody>{snapshot.simulated_events?.map(item => <tr key={item.id}>
          <td>{item.kind.replaceAll('_', ' ')}</td>
          <td>{item.dispatch_id.slice(0, 8)} / {item.segment_index}</td>
          <td>{item.speed_kph == null ? 'Road closed' : item.speed_kph + ' km/h'}</td>
          <td>{time(item.expires_at)}</td>
        </tr>)}</tbody>
      </table></div>
      <p className="muted">The backend can re-rank matching EV routes using these disruptions. Existing issued
        commands are not silently changed. A closure requires a safe alternative route before new approval;
        missing alternatives are rejected.</p>
    </>}
    {!simulated && !live && <p className="muted">
      Live LTA traffic ranking is disabled. Use the local Simulation Console for synthetic route disruptions
      (requires enabled operator control), or configure LTA DataMall for observed traffic.
      No conditions are assumed clear when data is unavailable.</p>}
    {live && <>
      <p className="muted">LTA DataMall observed traffic · snapshot {time(snapshot.generated_at)}.
        Available speeds and incidents are observations, not guaranteed road closures.</p>
      <div className="table-scroll"><table><thead><tr><th>Feed</th><th>Status</th><th>Observations</th><th>Last received</th></tr></thead>
        <tbody>{Object.entries(snapshot.feeds).map(([name, feed]) =>
          <tr key={name}><td>{name}</td><td>{feed.status}</td><td>{feed.count}</td><td>{time(feed.observed_at)}</td></tr>)}</tbody>
      </table></div>
      <h3>Geolocated incidents ({issues.length})</h3>
      {!issues.length && <p className="muted">No usable incident observations. Roads may still be disrupted.</p>}
      {issues.length > 0 && <div className="table-scroll"><table><thead><tr><th>Type</th><th>Message</th></tr></thead>
        <tbody>{issues.slice(0,20).map(i => <tr key={i.id}><td>{i.type}</td><td>{i.message}</td></tr>)}</tbody></table></div>}
      <h3>Approved works active today ({active.length})</h3>
      {!active.length && <p className="muted">No active works in the available feed. Roadworks are not automatic closures.</p>}
      {active.length > 0 && <div className="table-scroll"><table><thead><tr><th>Road</th><th>Until</th></tr></thead>
        <tbody>{active.slice(0,20).map(w => <tr key={w.id}><td>{w.road}</td><td>{w.end_date}</td></tr>)}</tbody></table></div>}
      <p className="muted">Roadworks are only listed by reported road name; no geometry is fabricated.
        Missing LTA speed coverage remains unknown, not free-flow traffic.</p>
    </>}
  </section>;
}
