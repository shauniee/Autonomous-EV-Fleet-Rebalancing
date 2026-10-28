import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import type { Dispatch, Facility, Vehicle, Zone } from '../api/contracts';
import { RouteDetails } from './RouteDetails';
const colours = { NORMAL: '#258371', WARNING: '#bc790b', CRITICAL: '#ce5147', UNKNOWN: '#76838a' };
const isChargingSite = (facility: Facility) => facility.facility_type === 'CHARGING' || (facility.charging_bays ?? 0) > 0;

export function SingaporeMap({ zones, vehicles, facilities = [], dispatches = [], selected, onSelect, selectedDispatch, onSelectDispatch }: {
  zones: Zone[]; vehicles: Vehicle[]; facilities?: Facility[]; dispatches?: Dispatch[]; selected: string; onSelect: (id: string) => void;
  selectedDispatch: string; onSelectDispatch: (id: string) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const fitted = useRef(false);
  const [tileError, setTileError] = useState(false);
  const [visible, setVisible] = useState({ zones: true, vehicles: true, parking: true, charging: true, routes: true });
  const activeRoutes = useMemo(() => dispatches.filter(dispatch => dispatch.command?.route
    && !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(dispatch.state)), [dispatches]);
  const journey = dispatches.find(dispatch => dispatch.dispatch_id === selectedDispatch && dispatch.command?.route) ?? activeRoutes.at(-1);
  const journeyId = journey?.dispatch_id;
  const trackedVehicle = vehicles.find(vehicle => vehicle.vehicle_id === journey?.vehicle_id);
  const [followEV, setFollowEV] = useState(false);
  const fittedJourney = useRef('');
  const routes = journey && !activeRoutes.includes(journey) ? [...activeRoutes, journey] : activeRoutes;
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
    { key: 'routes', label: 'Routes', colour: '#326995', count: routes.length },
  ] as const;
  useEffect(() => {
    const instance = L.map(element.current!, { preferCanvas: true }).setView([1.35, 103.82], 11);
    map.current = instance;
    L.tileLayer(import.meta.env.VITE_TILE_URL || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom: 18,
    }).on('tileerror', () => setTileError(true)).addTo(instance);
    const resize = new ResizeObserver(() => {
      instance.invalidateSize();
    });
    resize.observe(element.current!);
    return () => { resize.disconnect(); instance.remove(); map.current = null; fitted.current = false; fittedJourney.current = ''; };
  }, []);
  useEffect(() => {
    const route = journey?.command?.route;
    if (!map.current || !route || !journey || fittedJourney.current === journey.dispatch_id) return;
    fittedJourney.current = journey.dispatch_id;
    if (selectedDispatch !== journey.dispatch_id) onSelectDispatch(journey.dispatch_id);
    setVisible(current => ({ ...current, routes: true, vehicles: true }));
    map.current.fitBounds(route.geometry.map(point => [point.latitude, point.longitude]), { padding: [35, 35], maxZoom: 16 });
    // Fit once per selected journey; telemetry refreshes must preserve the operator's zoom.
  }, [journey, selectedDispatch, onSelectDispatch]);
  useEffect(() => {
    const telemetry = trackedVehicle?.telemetry;
    if (followEV && visible.vehicles && !trackedVehicle?.stale && telemetry && telemetry.active_dispatch_id === journeyId) {
      map.current?.panTo([telemetry.position.latitude, telemetry.position.longitude], { animate: false });
    }
  }, [followEV, visible.vehicles, trackedVehicle, journeyId]);
  useEffect(() => {
    if (!map.current) return;
    if (!fitted.current && zones.length) {
      if (!fittedJourney.current) map.current.fitBounds(zones.map(z => [z.position.latitude, z.position.longitude]), { padding: [30, 30], maxZoom: 12, animate: false });
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
    if (!map.current || !visible.routes) return;
    const layer = L.layerGroup().addTo(map.current);
    for (const dispatch of routes) {
      const route = dispatch.command!.route!;
      const focused = dispatch.dispatch_id === journeyId;
      const label = document.createElement('span');
      label.textContent = `${dispatch.vehicle_id} · OneMap road route · ${route.distance_km.toFixed(2)} km`;
      L.polyline(route.geometry.map(point => [point.latitude, point.longitude] as [number, number]), {
        color: focused ? '#176d62' : '#326995', weight: focused ? 5 : 3, opacity: focused ? .95 : .4,
      }).bindTooltip(label).on('click', () => onSelectDispatch(dispatch.dispatch_id)).addTo(layer);
      if (focused) {
        for (const [point, text] of [[route.geometry[0], 'Route start'], [route.geometry.at(-1)!, 'Parking / charging destination']] as const) {
          const endpoint = document.createElement('span'); endpoint.textContent = text;
          L.circleMarker([point.latitude, point.longitude], { radius: 6, color: '#176d62', fillColor: '#fff', fillOpacity: 1, weight: 2 })
            .bindTooltip(endpoint).addTo(layer);
        }
      }
    }
    return () => { layer.remove(); };
  }, [routes, journeyId, onSelectDispatch, visible.routes]);
  useEffect(() => {
    if (!map.current || !visible.vehicles) return;
    const layer = L.layerGroup().addTo(map.current);
    for (const vehicle of vehicles) {
      if (!vehicle.telemetry) continue;
      const label = document.createElement('span');
      label.textContent = `${vehicle.vehicle_id} · ${vehicle.telemetry.soc_pct.toFixed(1)}% · ${vehicle.connectivity}${vehicle.stale ? ' · STALE' : ''}${vehicle.active_rental ? ' · RENTED' : ''}`;
      const focused = vehicle.vehicle_id === journey?.vehicle_id;
      const marker = L.circleMarker([vehicle.telemetry.position.latitude, vehicle.telemetry.position.longitude], {
        radius: focused ? 10 : 7, color: focused ? '#176d62' : '#fff', weight: focused ? 3 : 2, fillColor: vehicle.stale ? '#76838a' : vehicle.active_rental ? '#ba791d' : '#163c63', fillOpacity: 1,
      }).bindTooltip(label, { permanent: focused, direction: 'top' }).addTo(layer);
      marker.on('click', () => {
        const dispatch = activeRoutes.find(item => item.vehicle_id === vehicle.vehicle_id);
        if (dispatch) onSelectDispatch(dispatch.dispatch_id);
      });
    }
    return () => { layer.remove(); };
  }, [vehicles, visible.vehicles, journey?.vehicle_id, activeRoutes, onSelectDispatch]);
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
        setFollowEV(false);
        if (zones.length) map.current?.fitBounds(zones.map(zone => [zone.position.latitude, zone.position.longitude]), { padding: [30, 30], maxZoom: 12 });
        else map.current?.setView([1.35, 103.82], 11);
      }}>Reset view</button>
    </div>
    <div className="route-toolbar">
      <label className="map-scope"><span>OneMap journey</span><span className="select-wrap">
        <select className="form-select" value={journeyId ?? ''} disabled={!dispatches.some(dispatch => dispatch.command?.route)} onChange={event => onSelectDispatch(event.target.value)}>
          {!journey && <option value="">No active road route</option>}
          {[...dispatches].reverse().filter(dispatch => dispatch.command?.route).map(dispatch => <option key={dispatch.dispatch_id} value={dispatch.dispatch_id}>
            {dispatch.vehicle_id} · {dispatch.command?.target_zone_id} · {dispatch.state} · {dispatch.dispatch_id.slice(0, 8)}
          </option>)}
        </select>
      </span></label>
      <label className="map-filter" data-active={followEV}><input type="checkbox" checked={followEV} disabled={!journey} onChange={event => setFollowEV(event.target.checked)}/>Follow EV</label>
      <button className="map-reset" disabled={!journey} onClick={() => {
        const route = journey?.command?.route;
        if (route) {
          setFollowEV(false); setVisible(current => ({ ...current, routes: true, vehicles: true }));
          map.current?.fitBounds(route.geometry.map(point => [point.latitude, point.longitude]), { padding: [35, 35], maxZoom: 16 });
        }
      }}>Fit route</button>
    </div>
    <div ref={element} className="map" aria-label="Singapore zones, fleet, and parking or charging sites map"/>
    {journey ? <RouteDetails dispatch={journey} vehicle={trackedVehicle}/> : <p className="map-filter-note">Approve a relocation with OneMap enabled to see its road route and follow the EV. Routes appear after approval.</p>}
    {!Object.values(visible).some(Boolean) && <p className="map-filter-note" role="status">All layers are hidden. Select a layer above to show map items.</p>}
    {tileError && <p className="warning" role="status">Map tiles unavailable. Use the zone list to select a destination.</p>}
  </>;
}
