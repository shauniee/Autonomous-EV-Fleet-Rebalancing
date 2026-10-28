import type { Vehicle, Zone } from '../api/contracts';
import { time } from './format';

export function RentalOutlook({ vehicles, zones }: { vehicles: Vehicle[]; zones: Zone[] }) {
  const horizon = Date.now() + 30 * 60_000;
  const now = Date.now();
  const rentals = vehicles.filter(v => v.active_rental);
  const candidates = rentals.filter(v => {
    const rental = v.active_rental!;
    const eta = Date.parse(rental.expected_end_at);
    return !!rental.expected_return_zone_id && !v.stale && v.connectivity === 'ONLINE'
      && Number.isFinite(eta) && eta >= now && eta <= horizon
      && typeof v.telemetry?.soc_pct === 'number' && v.telemetry.soc_pct >= 30;
  });
  return <section className="panel" aria-label="Anticipated customer returns">
    <div className="section-title"><h2>Customer rental return outlook</h2>
      <span className="muted">{rentals.length} currently rented · {candidates.length} potential returns within 30 min</span></div>
    <p className="muted">Expected destinations and times are estimates from the synthetic rental simulator, not confirmed parking or usable supply. Rental EVs are excluded from dispatch until Fleet confirms an actual return.</p>
    <div className="table-scroll"><table><thead><tr><th>Zone</th><th>Backend projected shortage</th><th>Potential customer returns</th><th>Illustrative gap if all return</th></tr></thead>
      <tbody>{zones.map(z => {
        const inbound = candidates.filter(v => v.active_rental?.expected_return_zone_id === z.zone_id).length;
        return <tr key={z.zone_id}><td>{z.name}</td><td>{z.projected_shortage ?? 'Unknown'}</td>
          <td>{inbound}</td><td>{z.projected_shortage == null ? 'Unknown' : Math.max(0, z.projected_shortage - inbound)}</td></tr>;
      })}</tbody></table></div>
    {rentals.length > 0 && <details><summary>Estimated rental returns</summary>
      <ul>{rentals.map(v => <li key={v.vehicle_id}>{v.vehicle_id} → {v.active_rental?.expected_return_zone_id ?? 'Unknown destination'} · expected {time(v.active_rental?.expected_end_at)}</li>)}</ul>
    </details>}
    <p className="muted">This panel does not alter recommendations, forecast demand, battery eligibility, charging allocation or dispatcher approval.</p>
  </section>;
}
