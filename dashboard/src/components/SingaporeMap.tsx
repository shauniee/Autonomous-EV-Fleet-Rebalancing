import { useEffect, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import type { Vehicle, Zone } from '../api/contracts';
const colours = { NORMAL: '#258371', WARNING: '#bc790b', CRITICAL: '#ce5147', UNKNOWN: '#76838a' };

export function SingaporeMap({ zones, vehicles, selected, onSelect }: {
  zones: Zone[]; vehicles: Vehicle[]; selected: string; onSelect: (id: string) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const latestZones = useRef(zones);
  latestZones.current = zones;
  const fitted = useRef(false);
  const [tileError, setTileError] = useState(false);
  useEffect(() => {
    const instance = L.map(element.current!).setView([1.35, 103.82], 11);
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
    for (const vehicle of vehicles) {
      if (!vehicle.telemetry) continue;
      const label = document.createElement('span');
      label.textContent = `${vehicle.vehicle_id} · ${vehicle.telemetry.soc_pct.toFixed(1)}% · ${vehicle.connectivity}${vehicle.stale ? ' · STALE' : ''}`;
      L.circleMarker([vehicle.telemetry.position.latitude, vehicle.telemetry.position.longitude], {
        radius: 6, color: '#fff', weight: 2, fillColor: vehicle.stale ? '#76838a' : '#163c63', fillOpacity: 1,
      }).bindTooltip(label).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [zones, vehicles, selected, onSelect]);
  return <><div ref={element} className="map" aria-label="Singapore zones and fleet map"/>{tileError && <p className="warning" role="status">Map tiles unavailable. Use the zone list to select a destination.</p>}</>;
}
