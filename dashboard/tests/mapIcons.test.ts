import { describe, expect, it } from 'vitest';
import { liveTrafficIconKind, mapIcon, simulatedTrafficIconKind } from '../src/components/mapIcons';

describe('traffic map icons', () => {
  it('uses dedicated symbols instead of demand heatmap circles', () => {
    expect(liveTrafficIconKind('ROAD BLOCK')).toBe('closure');
    expect(liveTrafficIconKind('Traffic Diversion')).toBe('diversion');
    expect(liveTrafficIconKind('Road Work')).toBe('roadworks');
    expect(liveTrafficIconKind('Accident')).toBe('incident');
    expect(simulatedTrafficIconKind('CONGESTION')).toBe('congestion');
    expect(simulatedTrafficIconKind('ROADWORKS')).toBe('roadworks');
    expect(simulatedTrafficIconKind('ROAD_CLOSED')).toBe('closure');
  });

  it('renders traffic events as shaped Leaflet pins', () => {
    const icon = mapIcon('closure');
    expect(icon.options.className).toContain('map-pin--closure');
    expect(icon.options.html).toContain('<circle');
  });
});
