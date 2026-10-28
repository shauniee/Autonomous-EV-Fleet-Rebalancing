import L from 'leaflet';

const svg = (paths: string) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;

// Static artwork only; facility names and telemetry never enter icon HTML.
export const mapSymbols = {
  ev: svg('<path d="m5 6-2 6v6h3m12 0h3v-6l-2-6H5Z"/><path d="M3 12h18M7 18h10M7 15h.01M17 15h.01M6 18v2m12-2v2m-6-13-2 3h4l-2 3"/>'),
  parking: svg('<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M9 18V7h4a3.5 3.5 0 0 1 0 7H9"/>'),
  charging: svg('<rect x="4" y="3" width="11" height="17" rx="2"/><path d="M2 21h15m-7-15-3 5h5l-3 5m6-8h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0V9l-3-3"/>'),
};

export function mapIcon(kind: keyof typeof mapSymbols, options: { muted?: boolean; selected?: boolean; rented?: boolean } = {}) {
  return L.divIcon({
    className: `map-pin map-pin--${kind}${options.muted ? ' is-muted' : ''}${options.selected ? ' is-selected' : ''}${options.rented ? ' is-rented' : ''}`,
    html: mapSymbols[kind], iconSize: [30, 30], iconAnchor: [15, 15], tooltipAnchor: [0, -17],
  });
}
