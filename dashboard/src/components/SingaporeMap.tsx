import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import 'leaflet.markercluster';
import 'leaflet.markercluster/dist/MarkerCluster.css';
import type { Dispatch, Facility, Position, TrafficSnapshot, Vehicle, Zone } from '../api/contracts';
import { RouteDetails } from './RouteDetails';
import { planConfirmedMotion, routeForVehicle, type MotionSample } from './confirmedMotion';
import { mapIcon, mapSymbols } from './mapIcons';
const colours = { NORMAL: '#258371', WARNING: '#bc790b', CRITICAL: '#ce5147', UNKNOWN: '#76838a' };
const isChargingSite = (facility: Facility) => facility.facility_type === 'CHARGING' || (facility.charging_bays ?? 0) > 0;
const reportedPositions = new WeakMap<L.Marker, L.LatLng>();

function nearestZone(position: Position, zones: Zone[]) {
  const point = L.latLng(position.latitude, position.longitude);
  let closest: Zone | undefined;
  let distance = Infinity;
  for (const zone of zones) {
    const next = point.distanceTo([zone.position.latitude, zone.position.longitude]);
    if (next < distance || (next === distance && zone.zone_id < (closest?.zone_id ?? ''))) {
      closest = zone; distance = next;
    }
  }
  return closest?.zone_id;
}

function removeHiddenPins(group: L.MarkerClusterGroup, pins: Map<string, L.Marker>, visibleIds: Set<string>) {
  const removed: L.Marker[] = [];
  for (const [id, pin] of pins) {
    if (!visibleIds.has(id)) { removed.push(pin); pins.delete(id); }
  }
  if (removed.length) group.removeLayers(removed);
}

function pinIconNeedsUpdate(pin: L.Marker, icon: L.DivIcon) {
  return pin.options.icon?.options.className !== icon.options.className;
}

function updatePin(pin: L.Marker, position: L.LatLngTuple, icon: L.DivIcon, label: HTMLElement) {
  const reported = L.latLng(position);
  // Expanded clusters temporarily move icons; compare reported coordinates so
  // an unchanged refresh does not pull those icons back into a stack.
  if (!reportedPositions.get(pin)?.equals(reported)) pin.setLatLng(reported);
  reportedPositions.set(pin, reported);
  if (pinIconNeedsUpdate(pin, icon)) pin.setIcon(icon);
  pin.setTooltipContent(label);
}

export function SingaporeMap({ zones, vehicles, facilities = [], dispatches = [], traffic, selected, onSelect, selectedDispatch, onSelectDispatch }: {
  zones: Zone[]; vehicles: Vehicle[]; facilities?: Facility[]; dispatches?: Dispatch[]; traffic?: TrafficSnapshot; selected: string; onSelect: (id: string) => void;
  selectedDispatch: string; onSelectDispatch: (id: string) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const markers = useRef<L.MarkerClusterGroup | null>(null);
  const facilityPins = useRef(new Map<string, L.Marker>());
  const vehiclePins = useRef(new Map<string, L.Marker>());
  const displayAnimations = useRef(new Map<string, number>());
  const lastConfirmed = useRef(new Map<string, MotionSample>());
  const fitted = useRef(false);
  const [tileError, setTileError] = useState(false);
  const [visible, setVisible] = useState({ zones: true, vehicles: true, parking: true, charging: true, routes: true, traffic: false });
  const [selectedZoneOnly, setSelectedZoneOnly] = useState(true);
  const areaZone = selectedZoneOnly ? selected : '';
  const area = zones.find(zone => zone.zone_id === areaZone);
  const shownZones = useMemo(() => areaZone ? zones.filter(zone => zone.zone_id === areaZone) : zones, [zones, areaZone]);
  const shownVehicles = useMemo(() => areaZone
    ? vehicles.filter(vehicle => vehicle.telemetry && nearestZone(vehicle.telemetry.position, zones) === areaZone)
    : vehicles, [vehicles, zones, areaZone]);
  const shownDispatches = useMemo(() => dispatches.filter(dispatch => dispatch.command?.route
    && (!areaZone || dispatch.command.target_zone_id === areaZone)), [dispatches, areaZone]);
  const activeRoutes = useMemo(() => shownDispatches.filter(dispatch => !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(dispatch.state)), [shownDispatches]);
  const journey = shownDispatches.find(dispatch => dispatch.dispatch_id === selectedDispatch) ?? activeRoutes.at(-1);
  const journeyId = journey?.dispatch_id;
  const trackedVehicle = shownVehicles.find(vehicle => vehicle.vehicle_id === journey?.vehicle_id);
  const [followEV, setFollowEV] = useState(false);
  const fittedJourney = useRef('');
  const routes = journey && !activeRoutes.includes(journey) ? [...activeRoutes, journey] : activeRoutes;
  const shownFacilities = useMemo(() => areaZone
    ? facilities.filter(facility => facility.zone_id === areaZone)
    : facilities, [facilities, areaZone]);
  const chargingCount = shownFacilities.filter(isChargingSite).length;
  const layers = [
    { key: 'zones', label: 'Demand zones', colour: '#258371', count: shownZones.length },
    { key: 'vehicles', label: 'EVs', colour: '#163c63', count: shownVehicles.filter(vehicle => vehicle.telemetry).length },
    { key: 'parking', label: 'Parking', colour: '#287d83', count: shownFacilities.filter(facility => facility.facility_type !== 'CHARGING').length },
    { key: 'charging', label: 'Charging', colour: '#a96817', count: chargingCount },
    { key: 'routes', label: 'Routes', colour: '#326995', count: routes.length },
    { key: 'traffic', label: 'Traffic', colour: '#b44d22', count: traffic?.source === 'LTA_DATAMALL' ? traffic.speeds.length + traffic.incidents.length : 0 },
  ] as const;
  useEffect(() => { setSelectedZoneOnly(true); setFollowEV(false); }, [selected]);
  useEffect(() => {
    const instance = L.map(element.current!, { preferCanvas: true, maxZoom: 18 }).setView([1.35, 103.82], 11);
    map.current = instance;
    markers.current = L.markerClusterGroup({
      maxClusterRadius: 48, showCoverageOnHover: false, spiderfyDistanceMultiplier: 1.7,
      animate: false,
      iconCreateFunction: cluster => {
        const selectedEV = cluster.getAllChildMarkers().some(marker => marker.options.icon?.options.className?.includes('is-selected'));
        const count = cluster.getChildCount().toLocaleString();
        return L.divIcon({
          className: `map-cluster${selectedEV ? ' is-selected' : ''}`,
          html: `${selectedEV ? mapSymbols.ev : ''}<strong>${count}</strong><span>items</span>`,
          iconSize: [44, 44], iconAnchor: [22, 22],
        });
      },
    }).addTo(instance);
    L.tileLayer(import.meta.env.VITE_TILE_URL || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom: 18,
    }).on('tileerror', () => setTileError(true)).addTo(instance);
    const resize = new ResizeObserver(() => {
      instance.invalidateSize();
    });
    resize.observe(element.current!);
    return () => {
      resize.disconnect(); instance.remove(); map.current = null; markers.current = null;
      for (const handle of displayAnimations.current.values()) cancelAnimationFrame(handle);
      displayAnimations.current.clear(); lastConfirmed.current.clear();
      facilityPins.current.clear(); vehiclePins.current.clear(); fitted.current = false; fittedJourney.current = '';
    };
  }, []);
  useEffect(() => {
    const route = journey?.command?.route;
    if (!map.current || !route || !journey || fittedJourney.current === journey.dispatch_id) return;
    fittedJourney.current = journey.dispatch_id;
    if (selectedDispatch !== journey.dispatch_id) onSelectDispatch(journey.dispatch_id);
    setVisible(current => ({ ...current, routes: true, vehicles: true }));
    const points = route.geometry.filter(point => !areaZone || nearestZone(point, zones) === areaZone);
    if (points.length) map.current.fitBounds(points.map(point => [point.latitude, point.longitude]), { padding: [35, 35], maxZoom: 16 });
    // Fit once per selected journey; telemetry refreshes must preserve the operator's zoom.
  }, [journey, selectedDispatch, onSelectDispatch, areaZone, zones]);
  useEffect(() => {
    if (area) map.current?.setView([area.position.latitude, area.position.longitude], 13);
    else if (fitted.current && zones.length) map.current?.fitBounds(zones.map(zone => [zone.position.latitude, zone.position.longitude]), { padding: [30, 30], maxZoom: 12 });
    // Keep manual zoom/pan when only telemetry or demand refreshes.
  }, [area?.zone_id, area?.position.latitude, area?.position.longitude]);
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
    for (const zone of shownZones) {
      // Text nodes prevent backend names from being interpreted as HTML.
      const label = document.createElement('span');
      label.textContent = `${zone.name} · ${zone.hotspot_severity}`;
      L.circleMarker([zone.position.latitude, zone.position.longitude], {
        radius: selected === zone.zone_id ? 19 : 14, color: selected === zone.zone_id ? '#143d42' : colours[zone.hotspot_severity],
        fillColor: colours[zone.hotspot_severity], fillOpacity: .65, weight: selected === zone.zone_id ? 4 : 2,
      }).bindTooltip(label).on('click', () => onSelect(zone.zone_id)).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [zones, shownZones, selected, onSelect, visible.zones]);
  useEffect(() => {
    const group = markers.current;
    if (!group) return;
    const added: L.Marker[] = [];
    const ids = new Set<string>();
    for (const facility of shownFacilities) {
      const hasChargers = isChargingSite(facility);
      if (!(visible.parking && facility.facility_type !== 'CHARGING') && !(visible.charging && hasChargers)) continue;
      const label = document.createElement('span');
      label.textContent = `${facility.name} · ${facility.source} · ${facility.status} · simulated availability ${facility.parking_available} · ${facility.facility_type === 'CHARGING' ? 'chargers' : 'parking spaces'}`;
      const position: L.LatLngTuple = [facility.position.latitude, facility.position.longitude];
      const icon = mapIcon(hasChargers && visible.charging ? 'charging' : 'parking', { muted: facility.status !== 'OPEN' });
      ids.add(facility.facility_id);
      const existing = facilityPins.current.get(facility.facility_id);
      if (existing) updatePin(existing, position, icon, label);
      else {
        const pin = L.marker(position, { icon, title: facility.name }).bindTooltip(label);
        reportedPositions.set(pin, L.latLng(position));
        facilityPins.current.set(facility.facility_id, pin); added.push(pin);
      }
    }
    removeHiddenPins(group, facilityPins.current, ids);
    if (added.length) group.addLayers(added);
  }, [shownFacilities, visible.parking, visible.charging]);
  useEffect(() => {
    if (!map.current || !visible.routes) return;
    const layer = L.layerGroup().addTo(map.current);
    for (const dispatch of routes) {
      const route = dispatch.command!.route!;
      const focused = dispatch.dispatch_id === journeyId;
      const label = document.createElement('span');
      label.textContent = `${dispatch.vehicle_id} · ${route.source} road route · ${route.distance_km.toFixed(2)} km${route.rerouted ? ' · traffic alternate' : ''}`;
      L.polyline(route.geometry.map(point => [point.latitude, point.longitude] as [number, number]), {
        color: focused ? '#176d62' : '#326995', weight: focused ? 5 : 3, opacity: focused ? .95 : .4,
      }).bindTooltip(label).on('click', () => onSelectDispatch(dispatch.dispatch_id)).addTo(layer);
      if (focused) {
        for (const [point, text] of [[route.geometry[0], 'Route start'], [route.geometry.at(-1)!, 'Parking / charging destination']] as const) {
          if (areaZone && nearestZone(point, zones) !== areaZone) continue;
          const endpoint = document.createElement('span'); endpoint.textContent = text;
          L.circleMarker([point.latitude, point.longitude], { radius: 6, color: '#176d62', fillColor: '#fff', fillOpacity: 1, weight: 2 })
            .bindTooltip(endpoint).addTo(layer);
        }
      }
    }
    return () => { layer.remove(); };
  }, [routes, journeyId, onSelectDispatch, visible.routes, areaZone, zones]);
  useEffect(() => {
    if (!map.current || !visible.traffic || traffic?.source !== 'LTA_DATAMALL') return;
    const layer = L.layerGroup().addTo(map.current);
    // Rendering bound: show at most 500 observed road segments at a time.
    // This layer is descriptive only; geometry is not a routable network.
    for (const segment of traffic.speeds.slice(0, 500)) {
      const average = (segment.min_kph + segment.max_kph) / 2;
      const colour = average < 20 ? '#bd453b' : average < 40 ? '#d6a137' : '#23826e';
      const label = document.createElement('span');
      label.textContent = segment.road + ' · ' + segment.min_kph + '–' + segment.max_kph + ' km/h (LTA observed)';
      L.polyline([[segment.start.latitude, segment.start.longitude], [segment.end.latitude, segment.end.longitude]],
        { color: colour, weight: 4, opacity: .7 }).bindTooltip(label).addTo(layer);
    }
    for (const incident of traffic.incidents.slice(0, 100)) {
      const label = document.createElement('span');
      label.textContent = incident.type + ': ' + incident.message;
      L.circleMarker([incident.position.latitude, incident.position.longitude],
        { radius: 7, color: '#ad3e28', fillOpacity: .75, weight: 2 })
        .bindTooltip(label).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [visible.traffic, traffic]);
  useEffect(() => {
    const group = markers.current;
    if (!group) return;
    const added: L.Marker[] = [];
    const ids = new Set<string>();
    for (const vehicle of shownVehicles) {
      if (!visible.vehicles || !vehicle.telemetry) continue;
      const label = document.createElement('span');
      label.textContent = `${vehicle.vehicle_id} · ${vehicle.telemetry.soc_pct.toFixed(1)}% · ${vehicle.connectivity}${vehicle.stale ? ' · STALE' : ''}${vehicle.active_rental ? ' · RENTED' : ''}`;
      const focused = vehicle.vehicle_id === journey?.vehicle_id;
      const position: L.LatLngTuple = [vehicle.telemetry.position.latitude, vehicle.telemetry.position.longitude];
      const icon = mapIcon('ev', { muted: vehicle.stale, selected: focused, rented: !!vehicle.active_rental });
      ids.add(vehicle.vehicle_id);
      let marker = vehiclePins.current.get(vehicle.vehicle_id);
      const current: MotionSample = vehicle.telemetry;
      const previous = lastConfirmed.current.get(vehicle.vehicle_id);
      const newObservation = !previous ||
        previous.session_id !== current.session_id ||
        previous.sequence !== current.sequence ||
        previous.observed_at !== current.observed_at;
      const roadPath = routeForVehicle(vehicle, dispatches);
      const active = displayAnimations.current.get(vehicle.vehicle_id);
      // A stale/offline/route-less observation must cancel any incomplete
      // easing, but an unrelated dashboard refresh must NOT restart it.
      const mustFreeze = !roadPath || vehicle.stale || vehicle.connectivity !== 'ONLINE';
      if (marker) {
        if (newObservation || (mustFreeze && active !== undefined)) {
          if (active !== undefined) {
            cancelAnimationFrame(active);
            displayAnimations.current.delete(vehicle.vehicle_id);
          }
          const plan = !mustFreeze ? planConfirmedMotion(previous, current, roadPath) : null;
          if (plan) {
            // Both endpoints are confirmed by Fleet and the path comes from
            // THIS vehicle's immutable Dispatch command, not the selected EV.
            marker.setIcon(icon);
            marker.setTooltipContent(label);
            reportedPositions.set(marker, L.latLng(position));
            const pin = marker;
            const begin = performance.now();
            const animate = (now: number) => {
              const progress = Math.min(1, Math.max(0, (now - begin) / plan.durationMs));
              const road = plan.positionAt(progress);
              if (road) pin.setLatLng([road.latitude, road.longitude]);
              if (progress < 1) {
                displayAnimations.current.set(vehicle.vehicle_id, requestAnimationFrame(animate));
              } else {
                pin.setLatLng(position);
                displayAnimations.current.delete(vehicle.vehicle_id);
              }
            };
            displayAnimations.current.set(vehicle.vehicle_id, requestAnimationFrame(animate));
          } else {
            // Stale, off-route, new session, missing geometry or a long
            // network gap: show the LAST confirmed position without guessing.
            updatePin(marker, position, icon, label);
          }
        } else {
          // Display-only state changes (zone, route selection, demand polling)
          // may update icons/labels but never restart a running animation.
          if (pinIconNeedsUpdate(marker, icon)) marker.setIcon(icon);
          marker.setTooltipContent(label);
          if (active === undefined) updatePin(marker, position, icon, label);
        }
      } else {
        marker = L.marker(position, { icon, title: vehicle.vehicle_id }).bindTooltip(label, { direction: 'top' });
        reportedPositions.set(marker, L.latLng(position));
        vehiclePins.current.set(vehicle.vehicle_id, marker); added.push(marker);
      }
      if (newObservation) lastConfirmed.current.set(vehicle.vehicle_id, { ...current });
      marker.off('click').on('click', () => {
        const dispatch = activeRoutes.find(item => item.vehicle_id === vehicle.vehicle_id);
        if (dispatch) onSelectDispatch(dispatch.dispatch_id);
      });
    }
    for (const id of vehiclePins.current.keys()) {
      if (!ids.has(id)) {
        const frame = displayAnimations.current.get(id);
        if (frame !== undefined) cancelAnimationFrame(frame);
        displayAnimations.current.delete(id);
        lastConfirmed.current.delete(id);
      }
    }
    removeHiddenPins(group, vehiclePins.current, ids);
    if (added.length) group.addLayers(added);
    group.refreshClusters();
  }, [shownVehicles, visible.vehicles, journey?.vehicle_id, dispatches, activeRoutes, onSelectDispatch]);
  return <>
    <div className="map-toolbar">
      <fieldset className="map-filters"><legend>Show on map</legend><div className="map-filter-options">
        {layers.map(({ key, label, colour, count }) => <label key={key} className="map-filter" data-active={visible[key]}>
          <input type="checkbox" checked={visible[key]} onChange={event => {
            const checked = event.currentTarget.checked;
            setVisible(current => ({ ...current, [key]: checked }));
          }}/>
          {key === 'vehicles' || key === 'parking' || key === 'charging'
            ? <span className="map-filter-symbol" style={{ color: colour }} aria-hidden="true" dangerouslySetInnerHTML={{ __html: mapSymbols[key === 'vehicles' ? 'ev' : key] }}/>
            : <span className="map-layer-dot" style={{ backgroundColor: colour }} aria-hidden="true"/>}
          <span>{label}</span><span className="map-layer-count">{count}</span>
        </label>)}
      </div></fieldset>
      <label className="map-scope"><span>Map area · all icons</span><span className="select-wrap">
        <select className="form-select" aria-label="Map area" value={areaZone ? 'selected' : 'all'} onChange={event => setSelectedZoneOnly(event.target.value === 'selected')}>
          <option value="all">All zones</option>
          <option value="selected" disabled={!selected}>{selected ? `${zones.find(zone => zone.zone_id === selected)?.name ?? selected} only` : 'Select a zone first'}</option>
        </select>
      </span></label>
      <button className="map-reset" onClick={() => {
        setFollowEV(false);
        if (area) map.current?.setView([area.position.latitude, area.position.longitude], 13);
        else if (zones.length) map.current?.fitBounds(zones.map(zone => [zone.position.latitude, zone.position.longitude]), { padding: [30, 30], maxZoom: 12 });
        else map.current?.setView([1.35, 103.82], 11);
      }}>Reset view</button>
    </div>
    <div className="route-toolbar">
      <label className="map-scope"><span>Road journey</span><span className="select-wrap">
        <select className="form-select" value={journeyId ?? ''} disabled={!shownDispatches.length} onChange={event => onSelectDispatch(event.target.value)}>
          {!journey && <option value="">No active road route</option>}
          {[...shownDispatches].reverse().map(dispatch => <option key={dispatch.dispatch_id} value={dispatch.dispatch_id}>
            {dispatch.vehicle_id} · {dispatch.command?.target_zone_id} · {dispatch.state} · {dispatch.dispatch_id.slice(0, 8)}
          </option>)}
        </select>
      </span></label>
      <label className="map-filter" data-active={followEV && !!trackedVehicle}><input type="checkbox" checked={followEV && !!trackedVehicle} disabled={!trackedVehicle} onChange={event => setFollowEV(event.target.checked)}/>Follow EV</label>
      <button className="map-reset" disabled={!journey} onClick={() => {
        const route = journey?.command?.route;
        if (route) {
          setFollowEV(false); setVisible(current => ({ ...current, routes: true, vehicles: true }));
          const points = route.geometry.filter(point => !areaZone || nearestZone(point, zones) === areaZone);
          if (points.length) map.current?.fitBounds(points.map(point => [point.latitude, point.longitude]), { padding: [35, 35], maxZoom: 16 });
        }
      }}>Fit route</button>
    </div>
    <div ref={element} className="map" aria-label="Singapore zones, fleet, and parking or charging sites map"/>
    <p className="map-marker-help">Nearby EVs, parking and chargers are grouped by count. Select a group to zoom in or spread overlapping locations apart. A car symbol in a group marks your selected EV.</p>
    {areaZone && <p className="map-marker-help">Showing icons in {area?.name ?? areaZone}. EVs use their reported location; inbound EVs appear when they enter this operating zone. Routes to this zone may extend beyond it. Choose All zones for the full fleet.</p>}
    {journey ? <RouteDetails dispatch={journey} vehicle={trackedVehicle}/> : <p className="map-filter-note">Approve a relocation with OneMap enabled to see its road route and follow the EV. Routes appear after approval.</p>}
    {!Object.values(visible).some(Boolean) && <p className="map-filter-note" role="status">All layers are hidden. Select a layer above to show map items.</p>}
    {tileError && <p className="warning" role="status">Map tiles unavailable. Use the zone list to select a destination.</p>}
  </>;
}
