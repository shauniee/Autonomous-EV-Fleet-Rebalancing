import { Fragment, useEffect, useMemo, useState } from 'react';
import type { FacilitiesSnapshot, Facility } from '../api/contracts';
import { time } from './format';

const PAGE_SIZE = 12;

export function FacilitiesPanel({ facilities, selectedZone, error, snapshot }: {
  facilities: Facility[]; selectedZone: string; error?: string; snapshot?: FacilitiesSnapshot | null;
}) {
  const [page, setPage] = useState(1);
  const visible = useMemo(() => selectedZone
    ? facilities.filter(site => site.zone_id === selectedZone).sort((a, b) =>
      a.name.localeCompare(b.name, 'en', { numeric: true, sensitivity: 'base' }) || a.facility_id.localeCompare(b.facility_id))
    : [], [facilities, selectedZone]);
  const pageCount = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount);
  const start = (currentPage - 1) * PAGE_SIZE;
  const pageFacilities = visible.slice(start, start + PAGE_SIZE);
  const pageNumbers = [...new Set([1, currentPage - 1, currentPage, currentPage + 1, pageCount])]
    .filter(value => value >= 1 && value <= pageCount).sort((a, b) => a - b);
  useEffect(() => { setPage(value => Math.min(value, pageCount)); }, [pageCount]);
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
    {visible.length > 0 && <nav className="facility-pagination" aria-label="Facilities pagination">
      <div className="pagination-summary"><span role="status"><strong>{start + 1}–{Math.min(start + PAGE_SIZE, visible.length)}</strong> of {visible.length} facilities</span><small>{PAGE_SIZE} facilities per page</small></div>
      <div className="pagination-controls">
        <div className="pagination-pages">
          <button disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)} aria-label="Previous facilities page"><span aria-hidden="true">‹</span></button>
          {pageNumbers.map((value, index) => <Fragment key={value}>
            {index > 0 && value - pageNumbers[index - 1] > 1 && <span className="pagination-gap" aria-hidden="true">…</span>}
            <button aria-label={`Facilities page ${value}`} aria-current={value === currentPage ? 'page' : undefined} onClick={() => setPage(value)}>{value}</button>
          </Fragment>)}
          <button disabled={currentPage === pageCount} onClick={() => setPage(currentPage + 1)} aria-label="Next facilities page"><span aria-hidden="true">›</span></button>
        </div>
        <label className="pagination-jump">Go to page <span className="select-wrap"><select className="form-select" aria-label="Facilities page" value={currentPage} onChange={event => setPage(Number(event.target.value))}>
          {Array.from({ length: pageCount }, (_, index) => <option key={index + 1} value={index + 1}>{index + 1}</option>)}
        </select></span></label>
      </div>
    </nav>}
    <div className="facility-grid">
      {pageFacilities.map(site => <article className="facility-card" key={site.facility_id} data-testid={`facility-${site.facility_id}`}>
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
