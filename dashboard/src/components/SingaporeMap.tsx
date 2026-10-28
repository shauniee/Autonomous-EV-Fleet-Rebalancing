import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import type { Facility, Vehicle, Zone } from '../api/contracts';
const colours = { NORMAL: '#258371', WARNING: '#bc790b', CRITICAL: '#ce5147', UNKNOWN: '#76838a' };
const isChargingSite = (facility: Facility) => facility.facility_type === 'CHARGING' || (facility.charging_bays ?? 0) > 0;

export function SingaporeMap({ zones, vehicles, facilities = [], selected, onSelect }: {
  zones: Zone[]; vehicles: Vehicle[]; facilities?: Facility[]; selected: string; onSelect: (id: string) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const latestZones = useRef(zones);
  latestZones.current = zones;
  const fitted = useRef(false);
  const [tileError, setTileError] = useState(false);
  const [visible, setVisible] = useState({ zones: true, vehicles: true, parking: true, charging: true });
  const [selectedZoneOnly, setSelectedZoneOnly] = useState(true);
  const facilityZone = selectedZoneOnly ? selected : '';
  const shownFacilities = useMemo(() => facilityZone
    ? facilities.filter(facility => facility.zone_id === facilityZone)
    : facilities, [facilities, facilityZone]);
  const chargingCount = shownFacilities.filter(isChargingSite).length;
  const layers = [
    { key: 'zones', label: 'Demand zones', colour: '#258371', count: zones.length },
    { key: 'vehicles', label: 'EVs', colour: '#163c63', count: vehicles.filter(vehicle => vehicle.telemetry).length },
    { key: 'parking', label: 'Parking', colour: '#287d83', count: shownFacilities.filter(facility => facility.facility_type !== 'CHARGING').length },
    { key: 'charging', label: 'Charging', colour: '#a96817', count: chargingCount },
  ] as const;
  useEffect(() => {
    const instance = L.map(element.current!, { preferCanvas: true }).setView([1.35, 103.82], 11);
    map.current = instance;
    L.tileLayer(import.meta.env.VITE_TILE_URL || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom: 18,
    }).on('tileerror', () => setTileError(true)).addTo(instance);
    const resize = new ResizeObserver(() => {
      instance.invalidateSize();
      if (latestZones.current.length) instance.fitBounds(latestZones.current.map(z => [z.position.latitude, z.position.longitude]), { padding: [30, 30], maxZoom: 12, animate: false });
    });
    resize.observe(element.current!);
    return () => { resize.disconnect(); instance.remove(); map.current = null; fitted.current = false; };
  }, []);
  useEffect(() => {
    if (!map.current) return;
    if (!fitted.current && zones.length) {
      map.current.fitBounds(zones.map(z => [z.position.latitude, z.position.longitude]), { padding: [30, 30], maxZoom: 12, animate: false });
      fitted.current = true;
    }
    if (!visible.zones) return;
    const layer = L.layerGroup().addTo(map.current);
    for (const zone of zones) {
      // Text nodes prevent backend names from being interpreted as HTML.
      const label = document.createElement('span');
      label.textContent = `${zone.name} · ${zone.hotspot_severity}`;
      L.circleMarker([zone.position.latitude, zone.position.longitude], {
        radius: selected === zone.zone_id ? 19 : 14, color: selected === zone.zone_id ? '#143d42' : colours[zone.hotspot_severity],
        fillColor: colours[zone.hotspot_severity], fillOpacity: .65, weight: selected === zone.zone_id ? 4 : 2,
      }).bindTooltip(label).on('click', () => onSelect(zone.zone_id)).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [zones, selected, onSelect, visible.zones]);
  useEffect(() => {
    if (!map.current || (!visible.parking && !visible.charging)) return;
    const layer = L.layerGroup().addTo(map.current);
    for (const facility of shownFacilities) {
      const hasChargers = isChargingSite(facility);
      if (!(visible.parking && facility.facility_type !== 'CHARGING') && !(visible.charging && hasChargers)) continue;
      const label = document.createElement('span');
      label.textContent = `${facility.name} · ${facility.source} · ${facility.status} · simulated availability ${facility.parking_available} · ${facility.facility_type === 'CHARGING' ? 'chargers' : 'parking spaces'}`;
      const markerColour = facility.status !== 'OPEN' ? '#76838a' : hasChargers && visible.charging ? '#a96817' : '#287d83';
      L.circleMarker([facility.position.latitude, facility.position.longitude], {
        radius: hasChargers ? 5 : 4, color: '#fff', weight: 1, fillColor: markerColour, fillOpacity: .85,
      }).bindTooltip(label).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [shownFacilities, visible.parking, visible.charging]);
  useEffect(() => {
    if (!map.current || !visible.vehicles) return;
    const layer = L.layerGroup().addTo(map.current);
    for (const vehicle of vehicles) {
      if (!vehicle.telemetry) continue;
      const label = document.createElement('span');
      label.textContent = `${vehicle.vehicle_id} · ${vehicle.telemetry.soc_pct.toFixed(1)}% · ${vehicle.connectivity}${vehicle.stale ? ' · STALE' : ''}${vehicle.active_rental ? ' · RENTED' : ''}`;
      L.circleMarker([vehicle.telemetry.position.latitude, vehicle.telemetry.position.longitude], {
        radius: 7, color: '#fff', weight: 2, fillColor: vehicle.stale ? '#76838a' : vehicle.active_rental ? '#ba791d' : '#163c63', fillOpacity: 1,
      }).bindTooltip(label).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [vehicles, visible.vehicles]);
  return <>
    <div className="map-toolbar">
      <fieldset className="map-filters"><legend>Show on map</legend><div className="map-filter-options">
        {layers.map(({ key, label, colour, count }) => <label key={key} className="map-filter" data-active={visible[key]}>
          <input type="checkbox" checked={visible[key]} onChange={event => {
            const checked = event.currentTarget.checked;
            setVisible(current => ({ ...current, [key]: checked }));
          }}/>
          <span className="map-layer-dot" style={{ backgroundColor: colour }} aria-hidden="true"/>
          <span>{label}</span><span className="map-layer-count">{count}</span>
        </label>)}
      </div></fieldset>
      <label className="map-scope"><span>Parking & charging area</span><span className="select-wrap">
        <select className="form-select" aria-label="Parking and charging area" value={facilityZone ? 'selected' : 'all'} onChange={event => setSelectedZoneOnly(event.target.value === 'selected')}>
          <option value="all">All zones</option>
          <option value="selected" disabled={!selected}>{selected ? `${zones.find(zone => zone.zone_id === selected)?.name ?? selected} only` : 'Select a zone first'}</option>
        </select>
      </span></label>
      <button className="map-reset" onClick={() => {
        if (zones.length) map.current?.fitBounds(zones.map(zone => [zone.position.latitude, zone.position.longitude]), { padding: [30, 30], maxZoom: 12 });
        else map.current?.setView([1.35, 103.82], 11);
      }}>Reset view</button>
    </div>
    <div ref={element} className="map" aria-label="Singapore zones, fleet, and parking or charging sites map"/>
    {!Object.values(visible).some(Boolean) && <p className="map-filter-note" role="status">All layers are hidden. Select a layer above to show map items.</p>}
    {tileError && <p className="warning" role="status">Map tiles unavailable. Use the zone list to select a destination.</p>}
  </>;
}
