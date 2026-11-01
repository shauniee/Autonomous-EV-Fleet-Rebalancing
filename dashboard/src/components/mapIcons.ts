import L from 'leaflet';

const svg = (paths: string) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;

// Static artwork only; facility names and telemetry never enter icon HTML.
export const mapSymbols = {
  ev: svg('<path d="m5 6-2 6v6h3m12 0h3v-6l-2-6H5Z"/><path d="M3 12h18M7 18h10M7 15h.01M17 15h.01M6 18v2m12-2v2m-6-13-2 3h4l-2 3"/>'),
  parking: svg('<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M9 18V7h4a3.5 3.5 0 0 1 0 7H9"/>'),
  charging: svg('<rect x="4" y="3" width="11" height="17" rx="2"/><path d="M2 21h15m-7-15-3 5h5l-3 5m6-8h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0V9l-3-3"/>'),
  congestion: svg('<path d="M4 6h16M4 12h10M4 18h16"/><path d="m16 9 4 3-4 3"/>'),
  roadworks: svg('<path d="M3 19h18M6 19l2-8h8l2 8M7 15h10M9 11l1-5h4l1 5"/>'),
  closure: svg('<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6 18.4 18.4"/>'),
  diversion: svg('<path d="M5 19V9a4 4 0 0 1 4-4h9"/><path d="m15 2 3 3-3 3"/><path d="M9 19h.01"/>'),
  incident: svg('<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5m0 3h.01"/>'),
};

export type TrafficIconKind = 'congestion' | 'roadworks' | 'closure' | 'diversion' | 'incident';

export function liveTrafficIconKind(type: string): TrafficIconKind {
  const value = type.toUpperCase();
  if (value.includes('ROAD BLOCK') || value.includes('CLOSURE')) return 'closure';
  if (value.includes('DIVERSION')) return 'diversion';
  if (value.includes('ROADWORK') || value.includes('ROAD WORK')) return 'roadworks';
  if (value.includes('CONGEST')) return 'congestion';
  return 'incident';
}

export function simulatedTrafficIconKind(kind: 'CONGESTION' | 'ROADWORKS' | 'ROAD_CLOSED'): TrafficIconKind {
  return kind === 'ROAD_CLOSED' ? 'closure' : kind === 'ROADWORKS' ? 'roadworks' : 'congestion';
}

export function mapIcon(kind: keyof typeof mapSymbols, options: { muted?: boolean; selected?: boolean; rented?: boolean } = {}) {
  return L.divIcon({
    className: `map-pin map-pin--${kind}${options.muted ? ' is-muted' : ''}${options.selected ? ' is-selected' : ''}${options.rented ? ' is-rented' : ''}`,
    html: mapSymbols[kind], iconSize: [30, 30], iconAnchor: [15, 15], tooltipAnchor: [0, -17],
  });
}
