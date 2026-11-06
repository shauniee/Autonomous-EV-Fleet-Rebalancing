import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import 'leaflet.markercluster';
import 'leaflet.markercluster/dist/MarkerCluster.css';
import type { Dispatch, Facility, Position, TrafficSnapshot, Vehicle, Zone } from '../api/contracts';
import { RouteDetails } from './RouteDetails';
import { planConfirmedMotion, routeForVehicle, type MotionSample } from './confirmedMotion';
import { liveTrafficIconKind, mapIcon, mapSymbols, simulatedTrafficIconKind } from './mapIcons';
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

function removeHiddenPins(group: L.LayerGroup, pins: Map<string, L.Marker>, visibleIds: Set<string>) {
  const removed: L.Marker[] = [];
  for (const [id, pin] of pins) {
    if (!visibleIds.has(id)) { removed.push(pin); pins.delete(id); }
  }
  if (group instanceof L.MarkerClusterGroup) {
    if (removed.length) group.removeLayers(removed);
  } else {
    for (const pin of removed) group.removeLayer(pin);
  }
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

export function SingaporeMap({ zones, vehicles, facilities = [], dispatches = [], traffic, telemetryFeedHealthy = true, selected, onSelect, selectedDispatch, onSelectDispatch, focusTarget }: {
  zones: Zone[]; vehicles: Vehicle[]; facilities?: Facility[]; dispatches?: Dispatch[]; traffic?: TrafficSnapshot; telemetryFeedHealthy?: boolean; selected: string; onSelect: (id: string) => void;
  selectedDispatch: string; onSelectDispatch: (id: string) => void;
  focusTarget?: { dispatchId: string } | { facility: Facility } | null;
}) {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const markers = useRef<L.MarkerClusterGroup | null>(null);
  const vehicleLayer = useRef<L.LayerGroup | null>(null);
  const facilityPins = useRef(new Map<string, L.Marker>());
  const vehiclePins = useRef(new Map<string, L.Marker>());
  const displayAnimations = useRef(new Map<string, number>());
  const lastConfirmed = useRef(new Map<string, MotionSample>());
  const fitted = useRef(false);
  const [mapVisible, setMapVisible] = useState(false);
  const [tileError, setTileError] = useState(false);
  const appliedFocus = useRef<typeof focusTarget>(null);
  const [visible, setVisible] = useState({ zones: true, vehicles: true, parking: true, charging: true, routes: true, traffic: true });
  const [selectedZoneOnly, setSelectedZoneOnly] = useState(false);
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
  const trackedVehicle = vehicles.find(vehicle => vehicle.vehicle_id === journey?.vehicle_id);
  const followUnavailable = !journey ? 'Select a road journey to follow an EV.'
    : ['SUCCEEDED', 'REJECTED', 'FAILED'].includes(journey.state) ? 'Journey ended.'
    : !telemetryFeedHealthy ? 'Fleet telemetry connection is unavailable.'
    : !trackedVehicle?.telemetry ? 'Vehicle telemetry is unavailable.'
    : trackedVehicle.stale ? 'Vehicle telemetry is stale.'
    : trackedVehicle.connectivity !== 'ONLINE' ? 'Vehicle is offline.'
    : trackedVehicle.telemetry.active_dispatch_id !== journeyId ? 'Waiting for telemetry for the selected journey.'
    : '';
  const [followEV, setFollowEV] = useState(false);
  const fittedJourney = useRef('');
  const routes = journey && !activeRoutes.includes(journey) ? [...activeRoutes, journey] : activeRoutes;
  const shownFacilities = useMemo(() => areaZone
    ? facilities.filter(facility => facility.zone_id === areaZone)
    : facilities, [facilities, areaZone]);
  const chargingCount = shownFacilities.filter(isChargingSite).length;
  const layers = [
    { key: 'zones', label: 'Demand', colour: '#258371', count: shownZones.length },
    { key: 'vehicles', label: 'EVs', colour: '#163c63', count: shownVehicles.filter(vehicle => vehicle.telemetry).length },
    { key: 'parking', label: 'Parking', colour: '#287d83', count: shownFacilities.filter(facility => facility.facility_type !== 'CHARGING').length },
    { key: 'charging', label: 'Charging', colour: '#a96817', count: chargingCount },
    { key: 'routes', label: 'Routes', colour: '#326995', count: routes.length },
    { key: 'traffic', label: 'Traffic', colour: '#b44d22', count:
      (traffic?.source === 'LTA_DATAMALL' ? traffic.speeds.length + traffic.incidents.length
        : (traffic?.live_observations?.speeds.length ?? 0) + (traffic?.live_observations?.incidents.length ?? 0))
      + (traffic?.simulated_events?.length ?? 0) },
  ] as const;
  useEffect(() => { setFollowEV(false); }, [selected]);
  useEffect(() => {
    const instance = L.map(element.current!, { preferCanvas: true, maxZoom: 18 }).setView([1.35, 103.82], 11);
    map.current = instance;
    instance.createPane('evPane').style.zIndex = '625';
    vehicleLayer.current = L.layerGroup().addTo(instance);
    markers.current = L.markerClusterGroup({
      maxClusterRadius: 48, showCoverageOnHover: false, spiderfyDistanceMultiplier: 1.7,
      animate: false,
      iconCreateFunction: cluster => {
        const count = cluster.getChildCount().toLocaleString();
        return L.divIcon({
          className: 'map-cluster',
          html: `<strong>${count}</strong><span>sites</span>`,
          iconSize: [44, 44], iconAnchor: [22, 22],
        });
      },
    }).addTo(instance);
    L.tileLayer(import.meta.env.VITE_TILE_URL || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom: 18,
    }).on('tileerror', () => setTileError(true)).addTo(instance);
    const resize = new ResizeObserver(() => {
      const visible = !!element.current?.clientWidth && !!element.current?.clientHeight;
      setMapVisible(visible);
      if (visible) instance.invalidateSize();
    });
    resize.observe(element.current!);
    return () => {
      for (const handle of displayAnimations.current.values()) cancelAnimationFrame(handle);
      resize.disconnect(); instance.remove(); map.current = null; markers.current = null; vehicleLayer.current = null;
      displayAnimations.current.clear(); lastConfirmed.current.clear();
      facilityPins.current.clear(); vehiclePins.current.clear(); fitted.current = false; fittedJourney.current = '';
    };
  }, []);
  useEffect(() => {
    const route = journey?.command?.route;
    if (!mapVisible || !map.current || !route || !journey || selectedDispatch !== journey.dispatch_id ||
        fittedJourney.current === journey.dispatch_id) return;
    fittedJourney.current = journey.dispatch_id;
    setVisible(current => ({ ...current, routes: true, vehicles: true }));
    if (followEV && !followUnavailable) return;
    const points = route.geometry.filter(point => !areaZone || nearestZone(point, zones) === areaZone);
    if (points.length) map.current.fitBounds(points.map(point => [point.latitude, point.longitude]), { padding: [35, 35], maxZoom: 16 });
    // Fit once per selected journey; telemetry refreshes must preserve the operator's zoom.
  }, [mapVisible, journey, selectedDispatch, areaZone, zones, followEV, followUnavailable]);
  useEffect(() => {
    // Following owns the camera when enabling it also changes the map area.
    if (followEV) return;
    if (area) map.current?.setView([area.position.latitude, area.position.longitude], 13);
    else if (fitted.current && zones.length) map.current?.fitBounds(zones.map(zone => [zone.position.latitude, zone.position.longitude]), { padding: [30, 30], maxZoom: 12 });
    // Keep manual zoom/pan when only telemetry or demand refreshes.
  }, [area?.zone_id, area?.position.latitude, area?.position.longitude]);
  useEffect(() => {
    if (!followEV) return;
    if (followUnavailable || !visible.vehicles || areaZone) {
      setFollowEV(false);
      return;
    }
    const telemetry = trackedVehicle?.telemetry;
    if (telemetry) {
      map.current?.panTo([telemetry.position.latitude, telemetry.position.longitude], { animate: false });
    }
  }, [followEV, followUnavailable, visible.vehicles, areaZone, trackedVehicle, journeyId]);
  useEffect(() => {
    if (!map.current) return;
    if (mapVisible && !fitted.current && zones.length) {
      if (!fittedJourney.current) map.current.fitBounds(zones.map(z => [z.position.latitude, z.position.longitude]), { padding: [30, 30], maxZoom: 12, animate: false });
      fitted.current = true;
    }
    if (!visible.zones) return;
    const layer = L.layerGroup().addTo(map.current);
    for (const zone of shownZones) {
      // Text nodes prevent backend names from being interpreted as HTML.
      const label = document.createElement('span');
      label.textContent = `${zone.name} · ${zone.hotspot_severity}`;
      const selectedZone = selected === zone.zone_id;
      const critical = zone.hotspot_severity === 'CRITICAL';
      L.circleMarker([zone.position.latitude, zone.position.longitude], {
        radius: critical ? 26 : selectedZone ? 23 : 14,
        color: colours[zone.hotspot_severity], fillColor: colours[zone.hotspot_severity],
        fillOpacity: selectedZone ? .58 : .5, weight: selectedZone ? 4 : 2,
      }).bindTooltip(label).on('click', () => onSelect(zone.zone_id)).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [mapVisible, zones, shownZones, selected, onSelect, visible.zones]);
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
        const pin = L.marker(position, { icon, title: facility.name })
          .bindTooltip(label, { direction: 'top', className: 'facility-map-tooltip', offset: [0, -5] });
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
    if (!map.current || !visible.traffic || !traffic) return;
    const layer = L.layerGroup().addTo(map.current);
    // Show full synthetic approved road segments, independently of LTA's
    // point-to-link observation overlay. No guessed geometry is plotted.
    for (const item of (traffic.simulated_events ?? []).slice(0, 30)) {
        const colour = item.kind === 'ROAD_CLOSED' ? '#c43332' : item.kind === 'ROADWORKS' ? '#e1a127' : '#eb762c';
        const description = document.createElement('span');
        description.textContent = `SIMULATED ${item.kind.replaceAll('_', ' ')} · ${item.speed_kph == null ? 'Road closed' : item.speed_kph + ' km/h'} · route ${item.dispatch_id.slice(0,8)} · segment ${item.segment_index} · expires ${new Date(item.expires_at).toLocaleTimeString('en-SG', {timeZone:'Asia/Singapore'})} SGT`;
        L.polyline([[item.start.latitude, item.start.longitude], [item.end.latitude, item.end.longitude]],
          { color: colour, weight: 9, opacity: .95, dashArray: item.kind === 'ROAD_CLOSED' ? '4,6' : undefined })
          .bindTooltip(description).bindPopup(description.cloneNode(true) as HTMLElement).addTo(layer);
        const midpoint: L.LatLngTuple = [
          (item.start.latitude + item.end.latitude) / 2,
          (item.start.longitude + item.end.longitude) / 2,
        ];
        L.marker(midpoint, {
          icon: mapIcon(simulatedTrafficIconKind(item.kind)),
          title: `Simulated ${item.kind.replaceAll('_', ' ').toLowerCase()}`,
        }).bindTooltip(description.cloneNode(true) as HTMLElement)
          .bindPopup(description.cloneNode(true) as HTMLElement).addTo(layer);
    }
    const observed = traffic.source === 'LTA_DATAMALL' ? traffic : traffic.live_observations;
    if (!observed) return () => { layer.remove(); };
    // The backend supplies a bounded, spatially representative projection.
    // This layer is descriptive only; geometry is not a routable network.
    for (const segment of observed.speeds.slice(0, 800)) {
      const average = (segment.min_kph + segment.max_kph) / 2;
      const colour = average < 20 ? '#bd453b' : average < 40 ? '#d6a137' : '#23826e';
      const label = document.createElement('span');
      label.textContent = segment.road + ' · ' + segment.min_kph + '–' + segment.max_kph + ' km/h (LTA observed)';
      L.polyline([[segment.start.latitude, segment.start.longitude], [segment.end.latitude, segment.end.longitude]],
        { color: colour, weight: 4, opacity: .7 }).bindTooltip(label).addTo(layer);
    }
    for (const incident of observed.incidents.slice(0, 100)) {
      const label = document.createElement('span');
      label.textContent = incident.type + ': ' + incident.message;
      L.marker([incident.position.latitude, incident.position.longitude], {
        icon: mapIcon(liveTrafficIconKind(incident.type)), title: `LTA ${incident.type}`,
      }).bindTooltip(label).bindPopup(label.cloneNode(true) as HTMLElement).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [visible.traffic, traffic]);
  useEffect(() => {
    const group = vehicleLayer.current;
    if (!group) return;
    const added: L.Marker[] = [];
    const ids = new Set<string>();
    for (const vehicle of shownVehicles) {
      if (!visible.vehicles || !vehicle.telemetry) continue;
      const label = document.createElement('span');
      label.textContent = `${vehicle.vehicle_id} · ${vehicle.telemetry.soc_pct.toFixed(1)}% · ${telemetryFeedHealthy ? vehicle.connectivity : 'TELEMETRY CONNECTION LOST'}${vehicle.stale ? ' · STALE' : ''}${vehicle.active_rental ? ' · RENTED' : ''}`;
      const focused = vehicle.vehicle_id === journey?.vehicle_id;
      const position: L.LatLngTuple = [vehicle.telemetry.position.latitude, vehicle.telemetry.position.longitude];
      const icon = mapIcon('ev', { muted: vehicle.stale || !telemetryFeedHealthy, selected: focused, rented: !!vehicle.active_rental });
      ids.add(vehicle.vehicle_id);
      let marker = vehiclePins.current.get(vehicle.vehicle_id);
      const current: MotionSample = vehicle.telemetry;
      const previous = lastConfirmed.current.get(vehicle.vehicle_id);
      const newObservation = !previous ||
        previous.session_id !== current.session_id ||
        previous.sequence !== current.sequence ||
        previous.observed_at !== current.observed_at;
      const roadPath = telemetryFeedHealthy ? routeForVehicle(vehicle, dispatches) : null;
      const active = displayAnimations.current.get(vehicle.vehicle_id);
      // A stale/offline/route-less observation must cancel any incomplete
      // easing, but an unrelated dashboard refresh must NOT restart it.
      const mustFreeze = !roadPath || !telemetryFeedHealthy || vehicle.stale || vehicle.connectivity !== 'ONLINE';
      if (marker) {
        if (newObservation || (mustFreeze && active !== undefined)) {
          if (active !== undefined) {
            cancelAnimationFrame(active);
            displayAnimations.current.delete(vehicle.vehicle_id);
          }
          const displayed = active !== undefined ? marker.getLatLng() : undefined;
          const plan = !mustFreeze ? planConfirmedMotion(previous, current, roadPath,
            displayed && { latitude: displayed.lat, longitude: displayed.lng }) : null;
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
            marker.setLatLng(position);
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
        marker = L.marker(position, { icon, title: vehicle.vehicle_id, pane: 'evPane' }).bindTooltip(label, { direction: 'top' });
        reportedPositions.set(marker, L.latLng(position));
        vehiclePins.current.set(vehicle.vehicle_id, marker); added.push(marker);
      }
      marker.setZIndexOffset(focused ? 1000 : 0);
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
    for (const pin of added) group.addLayer(pin);
  }, [shownVehicles, visible.vehicles, journey?.vehicle_id, dispatches, activeRoutes, onSelectDispatch, telemetryFeedHealthy]);
  useEffect(() => {
    const instance = map.current;
    if (!mapVisible || !instance || !focusTarget || appliedFocus.current === focusTarget) return;
    setFollowEV(false);
    if (areaZone) {
      setSelectedZoneOnly(false);
      return;
    }
    instance.invalidateSize({ pan: false });
    if ('facility' in focusTarget) {
      const site = focusTarget.facility;
      const layer = site.facility_type === 'CHARGING' ? 'charging' : 'parking';
      if (!visible[layer]) {
        setVisible(current => ({ ...current, [layer]: true }));
        return;
      }
      appliedFocus.current = focusTarget;
      instance.setView([site.position.latitude, site.position.longitude], 16, { animate: false });
      const pin = facilityPins.current.get(site.facility_id);
      if (pin) markers.current?.zoomToShowLayer(pin, () => pin.openTooltip());
    } else {
      const route = dispatches.find(dispatch => dispatch.dispatch_id === focusTarget.dispatchId)?.command?.route;
      if (!route?.geometry.length) return;
      appliedFocus.current = focusTarget;
      setVisible(current => ({ ...current, routes: true, vehicles: true }));
      instance.fitBounds(route.geometry.map(point => [point.latitude, point.longitude]), { padding: [35, 35], maxZoom: 16, animate: false });
    }
  }, [mapVisible, focusTarget, areaZone, dispatches, visible.parking, visible.charging]);
  return <>
    <div className="map-toolbar">
      <fieldset className="map-filters"><legend className="visually-hidden">Map layers</legend><div className="map-filter-options">
        {layers.map(({ key, label, colour, count }) => <label key={key} className="map-filter" data-active={visible[key]}>
          <input type="checkbox" checked={visible[key]} onChange={event => {
            const checked = event.currentTarget.checked;
            if (key === 'vehicles' && !checked) setFollowEV(false);
            setVisible(current => ({ ...current, [key]: checked }));
          }}/>
          {key === 'vehicles' || key === 'parking' || key === 'charging' || key === 'traffic'
            ? <span className="map-filter-symbol" style={{ color: colour }} aria-hidden="true" dangerouslySetInnerHTML={{ __html: mapSymbols[key === 'vehicles' ? 'ev' : key === 'traffic' ? 'incident' : key] }}/>
            : <span className="map-layer-dot" style={{ backgroundColor: colour }} aria-hidden="true"/>}
          <span>{label}</span><span className="map-layer-count">{count}</span>
        </label>)}
      </div></fieldset>
      <label className="map-scope"><span className="visually-hidden">Map area</span><span className="select-wrap">
        <select className="form-select" aria-label="Map area" value={areaZone ? 'selected' : 'all'} onChange={event => {
          const selectedOnly = event.target.value === 'selected';
          if (selectedOnly) setFollowEV(false);
          setSelectedZoneOnly(selectedOnly);
        }}>
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
    {shownDispatches.length > 0 && <div className="route-toolbar">
      <label className="map-scope"><span className="visually-hidden">Road journey</span><span className="select-wrap">
        <select className="form-select" value={journeyId ?? ''} disabled={!shownDispatches.length} onChange={event => onSelectDispatch(event.target.value)}>
          {!journey && <option value="">No active road route</option>}
          {[...shownDispatches].reverse().map(dispatch => <option key={dispatch.dispatch_id} value={dispatch.dispatch_id}>
            {dispatch.vehicle_id} → {dispatch.command?.target_zone_id} · {dispatch.state.replaceAll('_', ' ')}
          </option>)}
        </select>
      </span></label>
      <label className="map-filter" data-active={followEV && !followUnavailable}><input type="checkbox" checked={followEV && !followUnavailable} disabled={!!followUnavailable} aria-describedby={followUnavailable ? 'follow-ev-status' : undefined} onChange={event => {
        const checked = event.target.checked;
        if (checked) {
          setSelectedZoneOnly(false);
          setVisible(current => ({ ...current, vehicles: true }));
        }
        setFollowEV(checked);
      }}/>Follow EV</label>
      <button className="map-reset" disabled={!journey} onClick={() => {
        const route = journey?.command?.route;
        if (route) {
          setFollowEV(false); setVisible(current => ({ ...current, routes: true, vehicles: true }));
          const points = route.geometry.filter(point => !areaZone || nearestZone(point, zones) === areaZone);
          if (points.length) map.current?.fitBounds(points.map(point => [point.latitude, point.longitude]), { padding: [35, 35], maxZoom: 16 });
        }
      }}>Fit route</button>
    </div>}
    <div className="map-status">
      {journey && followUnavailable && <span id="follow-ev-status" className="follow-status" role="status">{followUnavailable}</span>}

      {!Object.values(visible).some(Boolean) && <p className="warning" role="status">All layers are hidden. Select a layer above to show map items.</p>}
      {!telemetryFeedHealthy && <p className="warning" role="status">Fleet telemetry stale or unavailable. Showing last confirmed positions.</p>}
      {tileError && <p className="warning" role="status">Map tiles unavailable. Select a destination from the zone list.</p>}
    </div>
    <div ref={element} className="map" aria-label="Singapore zones, fleet, and parking or charging sites map"/>
    <div className="map-context">
      {journey && <details><summary>{journey.vehicle_id} · {journey.state.replaceAll('_', ' ')} · Journey details</summary>
        <RouteDetails dispatch={journey} vehicle={trackedVehicle}/>
      </details>}
      <details className="map-help"><summary>Map help</summary>
        <p className="map-marker-help">Select a site cluster to zoom in or spread overlapping sites apart. EVs stay at their reported positions.</p>
        <p className="map-marker-help">Follow EV shows all zones. Selecting a single zone or hiding EVs stops following.</p>
      </details>
      {areaZone && <p className="map-marker-help">Showing {area?.name ?? areaZone} only. Switch to All zones for the full fleet and route.</p>}
    </div>
  </>;
}
