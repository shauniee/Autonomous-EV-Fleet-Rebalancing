import type { FacilitiesSnapshot, Facility } from '../api/contracts';
import { time } from './format';

export function FacilitiesPanel({ facilities, selectedZone, error, snapshot }: {
  facilities: Facility[]; selectedZone: string; error?: string; snapshot?: FacilitiesSnapshot | null;
}) {
  const visible = selectedZone ? facilities.filter(site => site.zone_id === selectedZone) : [];
  const live = snapshot?.data_source === 'LTA_DATAMALL';
  return <section className="panel" aria-label="Parking and charging facilities">
    <div className="section-title"><h2>Parking & charging facilities</h2><span className="mock-tag">{live ? 'LTA DATAMALL · SIMULATED ALLOCATIONS' : 'MOCK · NOT REAL PARKING'}</span></div>
    <p className="muted">{live
      ? 'Real LTA locations and availability observations. Allocations affect this simulation only; they do not book public parking or chargers. Locations are assigned to the nearest operating zone.'
      : 'Fictional parking locations and capacities. Batch recommendations evaluate and internally reserve simulated spaces only after approval. The legacy single-EV path still targets a zone.'}</p>
    {snapshot?.feeds?.map(feed => <p key={feed.name} className={feed.status === 'FRESH' ? 'muted' : 'warning'} role="status">
      LTA {feed.name}: {feed.status} · Last successful fetch {time(feed.last_success_at ?? undefined)}{feed.error_code ? ` · ${feed.error_code}` : ''}
    </p>)}
    {error && <p className="warning" role="status">Facilities service unavailable. {facilities.length ? 'Last known data shown.' : 'No locations loaded.'}</p>}
    {!selectedZone && <p className="empty">Select a zone above to inspect its parking and charging facilities. {facilities.length} map markers available.</p>}
    {selectedZone && !visible.length && <p className="empty">No facilities available for this zone.</p>}
    <div className="facility-grid">
      {visible.map(site => <article className="facility-card" key={site.facility_id} data-testid={`facility-${site.facility_id}`}>
        <div className="section-title"><h3>{site.name}</h3><span className="mock-tag">{site.source}</span></div>
        <p className="muted">{site.facility_id} · {site.status}</p>
        <dl className="candidate-metrics">
          {site.facility_type !== 'CHARGING' && <div><dt>Parking spaces available for simulation</dt><dd>{site.parking_available} / {site.parking_capacity ?? 'total unknown'}</dd></div>}
          <div><dt>{site.facility_type === 'CHARGING' ? 'Chargers available for simulation' : 'Charging bays available'}</dt><dd>{site.charging_bays == null ? 'Unknown' : `${site.charging_available} / ${site.charging_bays}`}</dd></div>
          {(site.charging_bays ?? 0) > 0 && <div><dt>Connector type</dt><dd>{site.connector_types.join(', ') || 'Unknown'}</dd></div>}
          {(site.charging_bays ?? 0) > 0 && <div><dt>Simulation charging rate</dt><dd>{site.charging_power_kw ?? 7.4} kW{site.charging_power_kw == null ? ' (assumed)' : ' (minimum advertised)'}</dd></div>}
          {site.source === 'LTA_DATAMALL' && <>
            <div><dt>Reported by LTA before simulated allocations</dt><dd>{site.reported_parking_available ?? site.reported_charging_available ?? 'Unknown'}</dd></div>
            <div><dt>Fetched at</dt><dd>{time(site.availability_observed_at ?? undefined)}</dd></div>
          </>}
        </dl>
        <small>{site.source === 'MOCK' ? 'Charging bays are included in total parking capacity.'
          : site.facility_type === 'CHARGING' ? 'One simulated vehicle per charger. General parking capacity and access are unknown.'
          : 'Total capacity and associated charging facilities are not supplied by this feed.'}</small>
      </article>)}
    </div>
  </section>;
}
