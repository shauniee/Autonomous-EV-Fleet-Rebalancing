import { describe, expect, it } from 'vitest';
import { interpolateConfirmedRoute } from '../src/components/routeInterpolation';

const start = { latitude: 1.3, longitude: 103.8 };
const corner = { latitude: 1.3, longitude: 103.81 };
const end = { latitude: 1.31, longitude: 103.81 };
const path = [start, corner, end];

describe('read-only road-geometry interpolation', () => {
  it('follows the actual L-shaped road rather than a flying diagonal', () => {
    const point = interpolateConfirmedRoute(path, start, end, .5);
    expect(point).not.toBeNull();
    expect(point!.latitude).toBeCloseTo(1.3, 3);
    expect(point!.longitude).toBeCloseTo(103.81, 3);
  });
  it('never projects past a later accepted observation', () => {
    const halfway = { latitude: 1.3, longitude: 103.805 };
    expect(interpolateConfirmedRoute(path, start, halfway, 1)).toEqual(halfway);
    expect(interpolateConfirmedRoute(path, start, halfway, 1.1)).toBeNull();
  });
  it('rejects off-road positions and reverse motion', () => {
    const offRoad = { latitude: 1.302, longitude: 103.805 };
    expect(interpolateConfirmedRoute(path, offRoad, end, .5)).toBeNull();
    expect(interpolateConfirmedRoute(path, end, start, .5)).toBeNull();
  });
});
