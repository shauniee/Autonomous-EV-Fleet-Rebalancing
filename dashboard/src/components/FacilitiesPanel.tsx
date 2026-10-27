import type { Facility } from '../api/contracts';

export function FacilitiesPanel({ facilities, selectedZone, error }: {
  facilities: Facility[]; selectedZone: string; error?: string;
}) {
  const visible = selectedZone ? facilities.filter(site => site.zone_id === selectedZone) : [];
  return <section className="panel" aria-label="Mock parking and charging facilities">
    <div className="section-title"><h2>Parking & charging facilities</h2><span className="mock-tag">SIMULATED · NOT RESERVABLE</span></div>
    <p className="muted">Fictional facility positions and capacities, for developing future destination selection. The single-EV recommendation remains zone-based. Bulk recommendations use these mock spaces and check capacity again on approval.</p>
    {error && <p className="warning" role="status">Facilities service unavailable. {facilities.length ? 'Last known mock data shown.' : 'No mock locations loaded.'}</p>}
    {!selectedZone && <p className="empty">Select a zone above to inspect its simulated parking and charging facilities. {facilities.length} mock map markers available.</p>}
    {selectedZone && !visible.length && <p className="empty">No mock facilities available for this zone.</p>}
    <div className="facility-grid">
      {visible.map(site => <article className="facility-card" key={site.facility_id} data-testid={`facility-${site.facility_id}`}>
        <div className="section-title"><h3>{site.name}</h3><span className="mock-tag">{site.source}</span></div>
        <p className="muted">{site.facility_id} · {site.status}</p>
        <dl className="candidate-metrics">
          <div><dt>Parking spaces available</dt><dd>{site.parking_available} / {site.parking_capacity}</dd></div>
          <div><dt>Charging bays available</dt><dd>{site.charging_available} / {site.charging_bays}</dd></div>
          {site.charging_bays > 0 && <div><dt>Connector type</dt><dd>{site.connector_types.join(', ')}</dd></div>}
        </dl>
        <small>Charging bays are included in total parking capacity.</small>
      </article>)}
    </div>
  </section>;
}
