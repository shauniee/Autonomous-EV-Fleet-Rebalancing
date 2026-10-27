import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

type Resource<T> = {
  data: T | null;
  observed_at: string | null;
  error: string | null;
};
type Zone = { zone_id: string; name: string };
type Vehicle = {
  vehicle_id: string;
  current_zone_id: string | null;
  connectivity: string;
  stale: boolean;
  effective_availability: string;
  telemetry: { soc_pct?: number } | null;
};
type Dispatch = {
  dispatch_id: string;
  vehicle_id: string | null;
  state: string;
  claim_held: boolean | null;
  command: { target_zone_id?: string } | null;
};
type Observations = {
  services: Record<string, Resource<{ status: string }>>;
  zones: Resource<Zone[]>;
  fleet: Resource<Vehicle[]>;
  dispatches: Resource<Dispatch[]>;
};
const services = ['fleet', 'demand', 'rebalancing', 'dispatch'];
const display = (value: unknown) =>
  typeof value === 'string' || typeof value === 'number'
    ? String(value)
    : 'Unknown';
function retain<T>(next: Resource<T>, previous?: Resource<T>): Resource<T> {
  return next.error && previous?.data !== null && previous?.data !== undefined
    ? { ...next, data: previous.data, observed_at: previous.observed_at }
    : next;
}

function App() {
  const [observations, setObservations] = useState<Observations | null>(null);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('Checking…');

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetch('/api/probe/v1/observations', {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(8000),
          ]),
        });
        if (!response.ok) throw new Error('Console request failed');
        const next: Observations = await response.json();
        if (controller.signal.aborted) return;
        setObservations((previous) => ({
          services: Object.fromEntries(
            services.map((name) => [
              name,
              retain(next.services[name], previous?.services[name]),
            ]),
          ),
          zones: retain(next.zones, previous?.zones),
          fleet: retain(next.fleet, previous?.fleet),
          dispatches: retain(next.dispatches, previous?.dispatches),
        }));
        setStatus('Available');
        setError('');
      } catch {
        if (controller.signal.aborted) return;
        setStatus('Unavailable');
        setError(
          'Console observations unavailable. Displayed observations are last known.',
        );
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, []);

  function freshness(resource?: Resource<unknown>) {
    if (!resource) return 'Waiting for observations';
    const time = resource.observed_at
      ? new Date(resource.observed_at).toLocaleTimeString('en-SG', {
          timeZone: 'Asia/Singapore',
        }) + ' SGT'
      : 'Never observed';
    return `${error || resource.error ? 'Last known / unavailable' : 'Last successful read'}: ${time}${resource.error ? ' · ' + resource.error : ''}`;
  }

  return (
    <main>
      <p className="eyebrow">EV FLEET</p>
      <h1>Simulation probe</h1>
      <p className="description">
        Read-only observations from the EV application. Updates every five
        seconds after each completed request.
      </p>
      <p className="health" role="status">
        Console status: <strong>{status}</strong>
      </p>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      <section>
        <h2>Service reachability</h2>
        <div className="services">
          {services.map((name) => {
            const resource = observations?.services[name];
            return (
              <div className="service" key={name}>
                <strong>{name}</strong>
                <p>
                  {error
                    ? 'Unknown — console unavailable'
                    : resource?.error
                      ? 'Unavailable'
                      : resource?.data
                        ? 'Reachable'
                        : 'Checking…'}
                </p>
                <small>{freshness(resource)}</small>
              </div>
            );
          })}
        </div>
        <p className="description">
          Reachability does not guarantee dependency health. Unknown values are
          not zero.
        </p>
      </section>
      <section>
        <h2>Operating zones</h2>
        <p className="description">Discovered from the app’s Demand API.</p>
        <p className="timestamp">{freshness(observations?.zones)}</p>
        <div className="zones">
          {observations?.zones.data?.map((zone) => (
            <div className="zone" key={zone.zone_id}>
              <strong>{zone.name}</strong>
              <small>{zone.zone_id}</small>
            </div>
          ))}
        </div>
        {!observations?.zones.data?.length && <p>No zones observed.</p>}
      </section>
      <section>
        <h2>Fleet</h2>
        <p className="timestamp">{freshness(observations?.fleet)}</p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Vehicle</th>
                <th>Zone</th>
                <th>Battery</th>
                <th>Availability</th>
                <th>Connectivity</th>
              </tr>
            </thead>
            <tbody>
              {observations?.fleet.data?.map((vehicle) => (
                <tr key={vehicle.vehicle_id}>
                  <td>{vehicle.vehicle_id}</td>
                  <td>{display(vehicle.current_zone_id)}</td>
                  <td>
                    {typeof vehicle.telemetry?.soc_pct === 'number'
                      ? vehicle.telemetry.soc_pct.toFixed(1) + '%'
                      : 'Unknown'}
                  </td>
                  <td>{vehicle.effective_availability}</td>
                  <td>
                    {vehicle.connectivity}
                    {vehicle.stale ? ' / stale' : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!observations?.fleet.data?.length && <p>No vehicles observed.</p>}
      </section>
      <section>
        <h2>Dispatch</h2>
        <p className="timestamp">{freshness(observations?.dispatches)}</p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Vehicle</th>
                <th>Destination</th>
                <th>State</th>
                <th>Claim</th>
              </tr>
            </thead>
            <tbody>
              {observations?.dispatches.data?.map((dispatch) => (
                <tr key={dispatch.dispatch_id}>
                  <td>{display(dispatch.vehicle_id)}</td>
                  <td>{display(dispatch.command?.target_zone_id)}</td>
                  <td>{dispatch.state}</td>
                  <td>
                    {dispatch.claim_held === null
                      ? 'Unknown'
                      : dispatch.claim_held
                        ? 'Held'
                        : 'Released'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!observations?.dispatches.data?.length && (
          <p>No dispatches observed.</p>
        )}
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
