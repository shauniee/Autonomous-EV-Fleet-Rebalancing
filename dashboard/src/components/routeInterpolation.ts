import type { Position } from '../api/contracts';

/** Display-only interpolation between two *confirmed* telemetry observations.
 * Never extrapolates beyond the latest accepted EV reading; movement remains
 * authoritative in the simulator. Returns null for off-route/retrograde data.
 */
function km(a: Position, b: Position): number {
  const lat = (a.latitude + b.latitude) * Math.PI / 360;
  const x = (b.longitude - a.longitude) * 111.195 * Math.cos(lat);
  const y = (b.latitude - a.latitude) * 111.195;
  return Math.hypot(x, y);
}
interface Projection { position: Position; at: number; offset: number }
function nearest(path: Position[], target: Position): Projection | null {
  if (path.length < 2) return null;
  let length = 0;
  let best: Projection | null = null;
  for (let i = 0; i < path.length - 1; i++) {
    const a = path[i], b = path[i + 1];
    const factor = Math.cos(target.latitude * Math.PI / 180);
    const dx = (b.longitude - a.longitude) * factor;
    const dy = b.latitude - a.latitude;
    const dot = dx * dx + dy * dy;
    const t = dot > 0 ? Math.max(0, Math.min(1,
      ((target.longitude - a.longitude) * factor * dx +
        (target.latitude - a.latitude) * dy) / dot)) : 0;
    const p = { latitude: a.latitude + t * (b.latitude - a.latitude),
      longitude: a.longitude + t * (b.longitude - a.longitude) };
    const offset = km(p, target);
    const candidate = { position: p, at: length + t * km(a, b), offset };
    if (!best || candidate.offset < best.offset) best = candidate;
    length += km(a, b);
  }
  return best;
}
function atDistance(path: Position[], progress: number): Position {
  let remaining = progress;
  for (let i = 0; i < path.length - 1; i++) {
    const a = path[i], b = path[i + 1], distance = km(a, b);
    if (remaining <= distance || i === path.length - 2) {
      const t = distance > 0 ? Math.max(0, Math.min(1, remaining / distance)) : 1;
      return { latitude: a.latitude + t * (b.latitude - a.latitude),
        longitude: a.longitude + t * (b.longitude - a.longitude) };
    }
    remaining -= distance;
  }
  return path.at(-1)!;
}

export function interpolateConfirmedRoute(path: Position[], from: Position,
                                           to: Position, progress: number): Position | null {
  if (!Number.isFinite(progress) || progress < 0 || progress > 1) return null;
  const first = nearest(path, from), second = nearest(path, to);
  // Don't draw an off-road line or pretend to know an alternate route.
  if (!first || !second || first.offset > .05 || second.offset > .05 ||
      second.at + .001 < first.at) return null;
  return atDistance(path, first.at + (second.at - first.at) * progress);
}
