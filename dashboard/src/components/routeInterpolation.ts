import type { Position } from '../api/contracts';

/** The simulator owns actual movement. This helper only eases between two
 * accepted positions on *the same approved road geometry*.
 */
function km(a: Position, b: Position): number {
  const lat = (a.latitude + b.latitude) * Math.PI / 360;
  return Math.hypot((b.longitude - a.longitude) * 111.195 * Math.cos(lat),
    (b.latitude - a.latitude) * 111.195);
}
interface Projection { at: number; offset: number }
function nearest(path: Position[], cumulative: number[], target: Position): Projection | null {
  let best: Projection | null = null;
  for (let i = 0; i < path.length - 1; i++) {
    const a = path[i], b = path[i + 1];
    const factor = Math.cos(target.latitude * Math.PI / 180);
    const dx = (b.longitude - a.longitude) * factor;
    const dy = b.latitude - a.latitude;
    const dot = dx * dx + dy * dy;
    const u = dot > 0 ? Math.max(0, Math.min(1,
      ((target.longitude - a.longitude) * factor * dx +
       (target.latitude - a.latitude) * dy) / dot)) : 0;
    const p = { latitude: a.latitude + u * (b.latitude - a.latitude),
      longitude: a.longitude + u * (b.longitude - a.longitude) };
    const offset = km(p, target);
    if (!best || offset < best.offset) {
      best = { at: cumulative[i] + u * (cumulative[i + 1] - cumulative[i]), offset };
    }
  }
  return best;
}
function roadPoint(path: Position[], cumulative: number[], at: number): Position {
  let lo = 0, hi = cumulative.length - 1;
  while (lo + 1 < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (cumulative[mid] <= at) lo = mid; else hi = mid;
  }
  const span = cumulative[lo + 1] - cumulative[lo];
  const ratio = span > 0 ? Math.max(0, Math.min(1, (at - cumulative[lo]) / span)) : 1;
  const a = path[lo], b = path[lo + 1];
  return { latitude: a.latitude + ratio * (b.latitude - a.latitude),
    longitude: a.longitude + ratio * (b.longitude - a.longitude) };
}

export function prepareConfirmedRoute(path: Position[], from: Position, to: Position):
  ((progress: number) => Position | null) | null {
  if (path.length < 2) return null;
  const cumulative = [0];
  for (let i = 0; i < path.length - 1; i++) cumulative.push(cumulative[i] + km(path[i], path[i+1]));
  const start = nearest(path, cumulative, from), end = nearest(path, cumulative, to);
  // A missing road match or apparent backwards movement is not guessed.
  if (!start || !end || start.offset > .05 || end.offset > .05 ||
      end.at + .001 < start.at) return null;
  return (progress: number) => {
    if (!Number.isFinite(progress) || progress < 0 || progress > 1) return null;
    return roadPoint(path, cumulative, start.at + (end.at - start.at) * progress);
  };
}
export function interpolateConfirmedRoute(path: Position[], from: Position,
                                           to: Position, progress: number): Position | null {
  return prepareConfirmedRoute(path, from, to)?.(progress) ?? null;
}
