import { describe, expect, it } from 'vitest';
import { makeDispatch, makeFleet } from './fixtures';
import { planConfirmedMotion, routeForVehicle } from '../src/components/confirmedMotion';

const a = { latitude: 1.3000, longitude: 103.8000 };
const corner = { latitude: 1.3000, longitude: 103.8100 };
const b = { latitude: 1.3100, longitude: 103.8100 };
const route = [a, corner, b];
const at = (sequence: number, position = a, observed_at = '2026-10-29T06:00:00Z') => ({
  position, sequence, observed_at, active_dispatch_id: 'dispatch-1', session_id: 'same-session',
});

describe('confirmed vehicle motion (read-only)', () => {
  it('matches the exact active dispatch of each EV, irrespective of map selection', () => {
    const first = makeFleet().vehicles[0];
    first.vehicle_id = 'ev-a';
    first.telemetry!.active_dispatch_id = 'dispatch-a';
    const second = structuredClone(first);
    second.vehicle_id = 'ev-b';
    second.telemetry!.active_dispatch_id = 'dispatch-b';
    const dA = makeDispatch();
    dA.vehicle_id = 'ev-a'; dA.dispatch_id = 'dispatch-a';
    dA.command!.route = {
      source: 'ONEMAP', fetched_at: '2026-10-29T06:00:00Z',
      geometry: route, distance_km: 2, duration_seconds: 200, instructions: [],
    };
    const dB = makeDispatch();
    dB.vehicle_id = 'ev-b'; dB.dispatch_id = 'dispatch-b';
    dB.command!.route = {
      source: 'ONEMAP', fetched_at: '2026-10-29T06:00:00Z',
      geometry: [b, corner, a], distance_km: 2, duration_seconds: 200, instructions: [],
    };
    expect(routeForVehicle(first, [dB, dA])).toEqual(route);
    expect(routeForVehicle(second, [dB, dA])).toEqual([b, corner, a]);
    // An EV cannot borrow another EV's route even if the dispatch ID is wrong.
    first.telemetry!.active_dispatch_id = 'dispatch-b';
    expect(routeForVehicle(first, [dB, dA])).toBeNull();
    first.telemetry!.active_dispatch_id = 'dispatch-a';
    first.stale = true;
    expect(routeForVehicle(first, [dB, dA])).toBeNull();
    first.stale = false;
    first.connectivity = 'OFFLINE';
    expect(routeForVehicle(first, [dB, dA])).toBeNull();
    first.connectivity = 'ONLINE';
    first.active_rental = { rental_id: 'test' } as typeof first.active_rental;
    expect(routeForVehicle(first, [dB, dA])).toBeNull();
  });

  it('eases around road corners between two accepted observations, with timestamp-based duration', () => {
    const plan = planConfirmedMotion(at(10, a), at(11, b, '2026-10-29T06:00:01Z'), route);
    expect(plan?.durationMs).toBe(900);
    expect(plan?.positionAt(0)).toEqual(a);
    const midpoint = plan!.positionAt(.5)!;
    expect(midpoint.latitude).toBeCloseTo(corner.latitude, 3);
    expect(midpoint.longitude).toBeCloseTo(corner.longitude, 3);
    expect(plan?.positionAt(1)).toEqual(b);
    // Never invent any motion beyond the most recently accepted observation.
    expect(plan?.positionAt(1.01)).toBeNull();
    expect(planConfirmedMotion(at(10, a), at(12, b, '2026-10-29T06:00:02Z'), route)?.durationMs).toBe(1400);
  });

  it('refuses a duplicate, sequence regression, new session or different dispatch', () => {
    const old = at(10);
    expect(planConfirmedMotion(old, at(10, b, '2026-10-29T06:00:01Z'), route)).toBeNull();
    expect(planConfirmedMotion(old, at(9, b, '2026-10-29T06:00:01Z'), route)).toBeNull();
    expect(planConfirmedMotion(old, { ...at(11, b, '2026-10-29T06:00:01Z'), session_id: 'new-session' }, route)).toBeNull();
    expect(planConfirmedMotion(old, { ...at(11, b, '2026-10-29T06:00:01Z'), active_dispatch_id: 'other' }, route)).toBeNull();
    expect(planConfirmedMotion(old, { ...at(11, b, '2026-10-29T06:00:01Z'), active_dispatch_id: null }, route)).toBeNull();
  });

  it('refuses missing/off-road geometry, clock regressions and long outage gaps', () => {
    const next = at(11, b, '2026-10-29T06:00:01Z');
    expect(planConfirmedMotion(at(10), next, null)).toBeNull();
    expect(planConfirmedMotion(at(10, { latitude: 1.302, longitude: 103.805 }), next, route)).toBeNull();
    expect(planConfirmedMotion(at(10), at(11, b, '2026-10-29T06:00:05Z'), route)).toBeNull();
    expect(planConfirmedMotion(at(10), at(11, b, '2026-10-29T05:59:59Z'), route)).toBeNull();
    expect(planConfirmedMotion(at(10), { ...next, observed_at: 'invalid' }, route)).toBeNull();
  });
});
