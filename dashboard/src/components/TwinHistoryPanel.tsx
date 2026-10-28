import { usePolling } from '../hooks/usePolling';
import type { Vehicle } from '../api/contracts';
import { number, time } from './format';

interface Sample {
  session_id: string; sequence: number; observed_at: string; received_at: string;
  telemetry: NonNullable<Vehicle['telemetry']>;
}
interface History {
  vehicle_id: string; storage: 'postgresql' | 'ephemeral';
  limit: number; samples: Sample[];
}

function Sparkline({ values, label }: {values: number[]; label: string}) {
  if (!values.length) return <span className="muted">No samples</span>;
  const hi = Math.max(...values), lo = Math.min(...values);
  const span = Math.max(.01, hi - lo);
  const path = values.map((v, i) => {
    const x = values.length === 1 ? 0 : i * 300 / (values.length - 1);
    const y = 65 - (v - lo) / span * 55;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  return <svg role="img" aria-label={label} viewBox="0 0 300 76" width="100%" height="76">
    <polyline fill="none" stroke="currentColor" strokeWidth="2" points={path}/>
  </svg>;
}

export function TwinHistoryPanel({ vehicle }: { vehicle: Vehicle }) {
  const readings = usePolling<History>(
    `/api/fleet/vehicles/${encodeURIComponent(vehicle.vehicle_id)}/telemetry-history?limit=100`, 10000);
  const samples = readings.data?.samples ?? [];
  const t = vehicle.telemetry;
  return <section className="panel" aria-label="EV digital twin diagnostics">
    <div className="section-title"><h2>Digital twin · {vehicle.vehicle_id}</h2>
      <span className="muted">{vehicle.stale ? 'STALE (last known)' : vehicle.connectivity}</span></div>
    <p className="muted">Only accepted edge-simulator telemetry controls the Fleet state. Chart samples are historical observations;
      smooth map motion is display-only and never commands an EV.</p>
    <dl className="candidate-metrics">
      <div><dt>Speed</dt><dd>{number(t?.speed_kph, ' km/h')}</dd></div>
      <div><dt>Heading</dt><dd>{number(t?.heading_deg, '°')}</dd></div>
      <div><dt>Odometer</dt><dd>{number(t?.odometer_km, ' km')}</dd></div>
      <div><dt>Battery SOH</dt><dd>{number(t?.battery_soh_pct, '%')}</dd></div>
      <div><dt>Battery temperature</dt><dd>{number(t?.battery_temp_c, '°C')}</dd></div>
      <div><dt>Motor temperature</dt><dd>{number(t?.motor_temp_c, '°C')}</dd></div>
      <div><dt>Power draw (+) / charging (−)</dt><dd>{number(t?.power_kw, ' kW')}</dd></div>
      <div><dt>GPS quality</dt><dd>{t?.gps_quality ?? 'Unknown'}</dd></div>
      <div><dt>Fault codes</dt><dd>{t?.fault_codes?.length ? t.fault_codes.join(', ') : 'None reported'}</dd></div>
    </dl>
    {readings.error && <p className="warning">Telemetry history unavailable: {readings.error}</p>}
    <p className="muted">{samples.length} accepted samples shown · {readings.data?.storage ?? 'Loading'} · latest {time(samples.at(-1)?.received_at)}</p>
    <div className="route-metrics">
      <div><strong>SOC (%)</strong><Sparkline label="Historical battery state of charge" values={samples.map(s => s.telemetry.soc_pct)}/></div>
      <div><strong>Speed (km/h)</strong><Sparkline label="Historical reported speed" values={samples.map(s => s.telemetry.speed_kph).filter((v): v is number => v != null)}/></div>
    </div>
    <small>All condition and GPS-quality measurements are simulated or derived, not calibrated hardware readings.
      PostgreSQL uses append-only session/sequence identities; offline demo mode keeps only a bounded in-memory history.</small>
  </section>;
}
