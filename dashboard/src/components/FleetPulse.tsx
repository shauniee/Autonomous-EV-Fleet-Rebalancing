import type { DispatchList, Fleet, HeatMap, Zone } from '../api/contracts';
import type { Resource } from '../hooks/usePolling';

import type { DemandLevel } from './DemandThresholds';

interface Props {
  rentalDemandLevel: (zone: Zone) => DemandLevel;
  fleet: Resource<Fleet>;
  heat: Resource<HeatMap>;
  dispatches: Resource<DispatchList>;
  now: number;
}

const MAX_OBSERVATION_AGE_MS = 5 * 60 * 1000;

function current<T>(resource: Resource<T>, now: number,
                    maxAgeMs = MAX_OBSERVATION_AGE_MS): resource is Resource<T> & { data: T } {
  return !!resource.data && !!resource.updatedAt &&
    Number.isFinite(Date.parse(resource.updatedAt)) &&
    now - Date.parse(resource.updatedAt) >= 0 &&
    now - Date.parse(resource.updatedAt) < maxAgeMs;
}

function observationDescription<T>(resource: Resource<T>, liveDescription: string) {
  return resource.error ? 'Last confirmed observation' : liveDescription;
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
export function FleetPulse({ fleet, heat, dispatches, now, rentalDemandLevel }: Props) {
  const vehicles = current(fleet, now) ? fleet.data.vehicles : null;
  const zones = current(heat, now) && heat.data.status === 'COMPLETE' ? heat.data.zones : null;
  const records = current(dispatches, now) ? dispatches.data.dispatches : null;

  const online = vehicles?.filter(v => !v.stale && v.connectivity === 'ONLINE').length ?? null;
  const available = vehicles?.filter(v => v.effective_availability === 'AVAILABLE').length ?? null;
  const critical = zones?.some(zone => rentalDemandLevel(zone) === 'Unknown') ? null :
    zones?.filter(zone => rentalDemandLevel(zone) === 'Critical').length ?? null;
  const open = records?.filter(d => !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)).length ?? null;

  return <section className="fleet-pulse" aria-labelledby="fleet-pulse-title">
    <div className="fleet-pulse-heading">
      <h2 id="fleet-pulse-title">Operational snapshot</h2>
      <p>Current service observations. Missing data is never shown as zero.</p>
    </div>
    <dl className="fleet-pulse-grid">
      <Metric label="Reporting EVs" value={online} description={observationDescription(fleet, 'Fresh telemetry')}/>
      <Metric label="Available EVs" value={available} description={observationDescription(fleet, 'Eligible availability state')} tone="accent"/>
      <Metric label="Open dispatches" value={open} description={observationDescription(dispatches, 'Including unresolved outcomes')}/>
      <Metric label="Critical zones" value={critical} description={observationDescription(heat, 'Demand hotspots')} tone={critical ? 'warning' : undefined}/>
    </dl>
  </section>;
}
