import type { Dispatch, Position, Vehicle } from '../api/contracts';
import { prepareConfirmedRoute } from './routeInterpolation';

export type MotionSample = Pick<NonNullable<Vehicle['telemetry']>,
  'position' | 'session_id' | 'sequence' | 'observed_at' | 'active_dispatch_id'>;

export interface MotionPlan {
  durationMs: number;
  positionAt: (progress: number) => Position | null;
}

export function routeForVehicle(vehicle: Vehicle, dispatches: Dispatch[]): Position[] | null {
  const id = vehicle.telemetry?.active_dispatch_id;
  if (!id || vehicle.stale || vehicle.connectivity !== 'ONLINE' || vehicle.active_rental) return null;
  const dispatch = dispatches.find(d => d.dispatch_id === id && d.vehicle_id === vehicle.vehicle_id);
  if (!dispatch?.command?.route || ['FAILED', 'REJECTED'].includes(dispatch.state)) return null;
  return dispatch.command.route.geometry;
}

/** No prediction: animate only the observed span of an immutable road route.
 * Refuse session changes, regressed samples, implausibly long gaps, or off-road
 * endpoints. The browser never writes operational position.
 */
export function planConfirmedMotion(
  previous: MotionSample | undefined, latest: MotionSample,
  path: Position[] | null,
): MotionPlan | null {
  if (!previous || !path || !latest.active_dispatch_id ||
      previous.active_dispatch_id !== latest.active_dispatch_id ||
      previous.session_id !== latest.session_id ||
      !Number.isSafeInteger(latest.sequence) || latest.sequence <= previous.sequence) return null;
  const elapsedMs = Date.parse(latest.observed_at) - Date.parse(previous.observed_at);
  if (!Number.isFinite(elapsedMs) || elapsedMs <= 0 || elapsedMs > 4000) return null;
  const positionAt = prepareConfirmedRoute(path, previous.position, latest.position);
  if (!positionAt) return null;
  return { positionAt, durationMs: Math.min(1400, Math.max(250, Math.round(elapsedMs * .9))) };
}
