import type { DispatchList, Fleet, HeatMap } from '../api/contracts';
import type { Resource } from '../hooks/usePolling';

interface Props {
  fleet: Resource<Fleet>;
  heat: Resource<HeatMap>;
  dispatches: Resource<DispatchList>;
  now: number;
}

function current<T>(resource: Resource<T>, now: number, maxAgeMs = 10000): resource is Resource<T> & { data: T } {
  return !!resource.data && !resource.error && !!resource.updatedAt &&
    Number.isFinite(Date.parse(resource.updatedAt)) &&
    now - Date.parse(resource.updatedAt) >= 0 &&
    now - Date.parse(resource.updatedAt) < maxAgeMs;
}

function Metric({ label, value, description, tone }: {
  label: string; value: number | null; description: string;
  tone?: 'accent' | 'warning';
}) {
  return <div className={`fleet-pulse-metric${tone ? ` fleet-pulse-metric--${tone}` : ''}`}>
    <dt>{label}</dt>
    <dd aria-label={value == null ? `${label}: unavailable` : undefined}>{value ?? '—'}</dd>
    <span className="fleet-pulse-description">{value == null ? 'Awaiting current data' : description}</span>
  </div>;
}

/** Read-only summary. Never use these display counts to enable dispatch approval. */
export function FleetPulse({ fleet, heat, dispatches, now }: Props) {
  const vehicles = current(fleet, now) ? fleet.data.vehicles : null;
  const zones = current(heat, now) && heat.data.status === 'COMPLETE' ? heat.data.zones : null;
  const records = current(dispatches, now) ? dispatches.data.dispatches : null;

  const online = vehicles?.filter(v => !v.stale && v.connectivity === 'ONLINE').length ?? null;
  const available = vehicles?.filter(v => v.effective_availability === 'AVAILABLE').length ?? null;
  const critical = zones?.filter(z => z.hotspot_severity === 'CRITICAL').length ?? null;
  const open = records?.filter(d => !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)).length ?? null;

  return <section className="fleet-pulse" aria-labelledby="fleet-pulse-title">
    <div className="fleet-pulse-heading">
      <h2 id="fleet-pulse-title">Operational snapshot</h2>
      <p>Current service observations. Missing data is never shown as zero.</p>
    </div>
    <dl className="fleet-pulse-grid">
      <Metric label="Vehicles online" value={online} description="Fresh telemetry"/>
      <Metric label="Available EVs" value={available} description="Eligible availability state" tone="accent"/>
      <Metric label="Open dispatches" value={open} description="Including unresolved outcomes"/>
      <Metric label="Critical zones" value={critical} description="Demand hotspots" tone={critical ? 'warning' : undefined}/>
    </dl>
  </section>;
}
