import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { DemandForm } from './DemandForm';
import { TrafficScenariosPanel } from './TrafficScenariosPanel';
import { RentalPanel } from './RentalPanel';
import { AnalyticsPanel } from './AnalyticsPanel';
import type { RentalVehicle } from './RentalPanel';
import type { Zone } from './DemandForm';
import { applyTheme, initialTheme, type Theme } from './theme';
import './styles.css';

type Resource<T> = {
  data: T | null;
  observed_at: string | null;
  error: string | null;
};
type Vehicle = RentalVehicle;
type Dispatch = {
  dispatch_id: string;
  vehicle_id: string | null;
  state: string;
  claim_held: boolean | null;
  command: { target_zone_id?: string; route?: {source: string; geometry: {latitude:number;longitude:number}[]} | null } | null;
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
  const [theme, setTheme] = useState<Theme>(() => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
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
      <header className="console-header">
        <div><p className="eyebrow">EV FLEET</p><h1>Simulation probe</h1></div>
        <div className="theme-toggle" role="group" aria-label="Color theme">
          <button type="button" className="theme-option" aria-label="Light mode" aria-pressed={theme === 'light'} onClick={() => { applyTheme('light', true); setTheme('light'); }}>
            <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg></span>Light
          </button>
          <button type="button" className="theme-option" aria-label="Dark mode" aria-pressed={theme === 'dark'} onClick={() => { applyTheme('dark', true); setTheme('dark'); }}>
            <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M20.5 13.2A8.5 8.5 0 0 1 10.8 3.5a8.5 8.5 0 1 0 9.7 9.7Z"/></svg></span>Dark
          </button>
        </div>
      </header>
      <p className="description">
        Configure zone demand and observe the EV application. Observations
        update every five seconds after each completed request.
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
      <DemandForm
        zones={observations?.zones.data ?? []}
        available={
          !!observations?.zones.data && !observations.zones.error && !error
        }
      />
      <TrafficScenariosPanel dispatches={observations?.dispatches.data ?? []}
        available={!error && !!observations?.dispatches.data && !observations.dispatches.error}/>
      <AnalyticsPanel />
      <RentalPanel vehicles={observations?.fleet.data ?? []} zones={observations?.zones.data ?? []}
        available={!error && !!observations?.fleet.data && !observations.fleet.error && !!observations?.zones.data && !observations.zones.error} />
      <section>
        <h2>Operating zones</h2>
        <p className="description">Discovered from the app’s Demand API.</p>
        <p className="timestamp">{freshness(observations?.zones)}</p>
        <div className="zones">
          {observations?.zones.data?.map((zone) => (
            <div className="zone" key={zone.zone_id}>
              <strong>{zone.name}</strong>
              <small>{zone.zone_id}</small>
              <small>
                Desired supply:{' '}
                {zone.demand?.desired_available_vehicles ?? 'Unknown'}
              </small>
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

applyTheme(initialTheme());
createRoot(document.getElementById('root')!).render(<App />);
