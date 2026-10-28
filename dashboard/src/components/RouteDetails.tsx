import type { Dispatch, Vehicle } from '../api/contracts';
import { Badge, number, time } from './format';

export function RouteDetails({ dispatch, vehicle }: { dispatch: Dispatch; vehicle?: Vehicle }) {
  const route = dispatch.command?.route;
  if (!route) return null;
  const telemetry = vehicle?.telemetry;
  const distance = telemetry?.active_dispatch_id === dispatch.dispatch_id && !vehicle?.stale
    ? telemetry.distance_travelled_km ?? dispatch.distance_travelled_km : dispatch.distance_travelled_km;
  const progress = dispatch.state === 'SUCCEEDED' ? 1
    : route.distance_km > 0 ? Math.min(1, Math.max(0, distance / route.distance_km)) : 0;
  const ended = ['SUCCEEDED', 'REJECTED', 'FAILED'].includes(dispatch.state);
  return <div className="route-details">
    <div className="section-title"><strong>{dispatch.vehicle_id} · OneMap driving route</strong><Badge value={dispatch.state}/></div>
    <div className="route-metrics">
      <span><strong>{number(route.distance_km, ' km')}</strong><small>Road distance</small></span>
      <span><strong>{Math.ceil(route.duration_seconds / 60)} min</strong><small>Estimated simulated journey</small></span>
      <span><strong>{Math.round(progress * 100)}%</strong><small>Reported progress</small></span>
    </div>
    <progress value={progress} max={1} aria-label={`${dispatch.vehicle_id} journey progress`}/>
    <p className="muted">{ended ? 'Journey ended.' : dispatch.state === 'UNKNOWN'
      ? 'Execution status uncertain; remaining time unavailable.'
      : `Estimated remaining travel: ${Math.ceil(route.duration_seconds * (1 - progress) / 60)} simulated min.`}
      {' '}Simulation time may be accelerated; live traffic adjustment is unverified.</p>
    <small>EV position reported {time(telemetry?.observed_at)}{vehicle?.stale ? ' · STALE — last known position' : ''} · Route fetched {time(route.fetched_at)}</small>
    <details className="route-directions"><summary>Driving directions · {route.instructions.length} steps</summary>
      {route.instructions.length ? <ol>{route.instructions.map((instruction, index) => <li key={index}>
        {instruction.text} <span className="muted">· {number(instruction.distance_m, ' m')}</span>
      </li>)}</ol> : <p className="muted">No turn instructions were returned. The road route is shown on the map.</p>}
    </details>
  </div>;
}
