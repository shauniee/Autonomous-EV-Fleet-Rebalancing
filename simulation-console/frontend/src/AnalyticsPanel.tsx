import { useEffect, useState } from 'react';

type Hpa = { current_cpu_pct: number | null; target_cpu_pct: number | null;
  current_replicas: number | null; desired_replicas: number | null; max_replicas: number | null };
type Sample = { timestamp: string; hpa: Hpa | null; attempted: number; success: number;
  errors: number; p95_latency_ms: number | null;
  usage: { cpu_millicores: number; memory_mib: number } | null };
type Experiment = { id: string; preset: string; state: string; started_at: string;
  duration_seconds: number; simulated_evs: number; telemetry_hz: number; requests_per_second: number;
  attempted: number; success: number; errors: number; p95_latency_ms: number | null;
  rps_observed: number; samples: Sample[] };
type Recorded = Pick<Experiment, 'id' | 'preset' | 'state' | 'started_at' | 'attempted' | 'errors'>;
type Infra = { observed_at: string; hpa: Hpa | null;
  pods: { ready: number; total: number } | null;
  usage: { cpu_millicores: number; memory_mib: number } | null;
  errors: Record<string, string> };
type Snapshot = { infrastructure: Infra; experiment: Experiment | null;
  load_enabled: boolean; storage: string };
const presets = [
  { id: 'NORMAL', title: 'Normal', evs: 10, hz: 0.2, rps: 2 },
  { id: 'HIGH', title: 'High', evs: 50, hz: 1, rps: 50 },
  { id: 'EXTREME', title: 'Extreme', evs: 100, hz: 2, rps: 200 },
] as const;
function fmt(v: number | null | undefined, suffix = '') {
  return v == null || !Number.isFinite(v) ? 'Unavailable' : String(v) + suffix;
}
function sgt(v?: string | null) {
  return v ? new Date(v).toLocaleTimeString('en-SG', {timeZone:'Asia/Singapore'}) + ' SGT' : '—';
}
function Graph({title, samples, measure, maxValue, target}: {
  title: string; samples: Sample[];
  measure: (s: Sample, index: number) => number | null | undefined;
  maxValue: number; target?: number | null;
}) {
  const x = (i: number) => 36 + (samples.length < 2 ? 0 : i * 480 / (samples.length - 1));
  const y = (n: number) => 116 - 100 * Math.min(1, Math.max(0, n / maxValue));
  const segments: string[] = [];
  let current = '';
  samples.forEach((s, i) => {
    const v = measure(s, i);
    if (v == null || !Number.isFinite(v)) {
      if (current) segments.push(current);
      current = '';
    } else {
      current += (current ? ' L ' : 'M ') + x(i).toFixed(1) + ' ' + y(v).toFixed(1);
    }
  });
  if (current) segments.push(current);
  return <div className="analytics-chart">
    <h4>{title}</h4>
    <svg viewBox="0 0 540 140" role="img" aria-label={title + ' history'}>
      {[0, 0.5, 1].map(frac => <g key={frac}>
        <line x1="36" x2="520" y1={y(frac * maxValue)} y2={y(frac * maxValue)}
          stroke="var(--border)"/>
        <text x="30" y={y(frac * maxValue)+4} textAnchor="end" fill="var(--muted)" fontSize="11">
          {Math.round(frac * maxValue)}
        </text></g>)}
      {target != null && <line x1="36" x2="520" y1={y(target)} y2={y(target)}
        stroke="var(--warning-text)" strokeDasharray="6 4" strokeWidth="2"/>}
      {segments.map((path,i) => <path key={i} d={path} fill="none"
        stroke="var(--focus)" strokeWidth="2.5"/>)}
      <text x="36" y="135" fontSize="11" fill="var(--muted)">Start</text>
      <text x="520" y="135" textAnchor="end" fontSize="11" fill="var(--muted)">Latest</text>
    </svg>
    {!segments.length && <small>No measured data yet (missing readings are not zero).</small>}
  </div>;
}
export function AnalyticsPanel() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [records, setRecords] = useState<Recorded[]>([]);
  const [duration, setDuration] = useState(60);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  async function getRecords() {
    try {
      const response = await fetch('/api/probe/v1/analytics/runs',
        {signal: AbortSignal.timeout(8000)});
      if (response.ok) {
        const data: {runs: Recorded[]} = await response.json();
        setRecords(data.runs);
      }
    } catch { /* Independent record list unavailable; status polling still works. */ }
  }
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetch('/api/probe/v1/analytics/status',
          {signal: AbortSignal.timeout(12000)});
        if (!response.ok) throw new Error('Metrics request failed: HTTP ' + response.status);
        const next: Snapshot = await response.json();
        if (!cancelled) { setSnapshot(next); setError(''); void getRecords(); }
      } catch (cause) {
        if (!cancelled) {
          setSnapshot(null);
          setError(cause instanceof Error ? cause.message : 'Metrics unavailable');
        }
      }
      if (!cancelled) timer = setTimeout(poll, 5000);
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);

  async function control(action: 'start' | 'stop', preset?: string) {
    setBusy(true); setMessage('');
    try {
      const response = await fetch('/api/probe/v1/analytics/' + action, {
        method: 'POST', signal: AbortSignal.timeout(8000),
        headers: preset ? {'Content-Type':'application/json'} : undefined,
        body: preset ? JSON.stringify({preset, duration_seconds:duration}) : undefined
      });
      if (!response.ok) {
        const body: {detail?: string} = await response.json().catch(() => ({}));
        throw new Error(typeof body.detail === 'string' ? body.detail : 'HTTP ' + response.status);
      }
      setMessage(action === 'start'
        ? 'Local synthetic dashboard traffic started. Fleet and dispatch state are not changed.'
        : 'Stopped. Experiment data is available for export.');
      await getRecords();
    } catch (cause) {
      setMessage('Action rejected: ' + (cause instanceof Error ? cause.message : 'unknown error'));
    } finally { setBusy(false); }
  }

  const infra = snapshot?.infrastructure;
  const experiment = snapshot?.experiment;
  const running = experiment?.state === 'RUNNING';
  const samples = experiment?.samples ?? [];
  const observedRps = samples.map((sample, index) => {
    if (!index) return null;
    const previous = samples[index - 1];
    const seconds = (Date.parse(sample.timestamp) - Date.parse(previous.timestamp)) / 1000;
    return seconds > 0 ? Math.max(0, (sample.attempted - previous.attempted) / seconds) : null;
  });
  return <section className="analytics-section" aria-labelledby="analytics-title">
    <div className="analytics-heading">
      <div><p className="eyebrow">SIMULATION · OBSERVABILITY</p>
        <h2 id="analytics-title">Performance analytics</h2></div>
      <span className="analytics-pill">{running ? 'Experiment running' : 'Infrastructure monitor'}</span>
    </div>
    <p className="description">Real Kubernetes metrics and HPA decisions alongside controlled synthetic
      dashboard HTTP load. Preset EV counts and update rates represent an equivalent workload only:
      no Fleet telemetry, rental or Dispatch commands are sent. Scaling is not guaranteed.</p>
    {error && <p className="warning" role="alert">{error}. Measurements cannot be verified.</p>}
    {infra && Object.keys(infra.errors).length > 0 && <p className="warning" role="status">
      Partial metrics: {Object.entries(infra.errors).map(([name, cause]) =>
        name + ': ' + cause + '. ').join('')}
    </p>}
    <div className="analytics-metrics">
      <div className="analytics-stat"><small>HPA CPU / target</small>
        <strong>{fmt(infra?.hpa?.current_cpu_pct, '%')} / {fmt(infra?.hpa?.target_cpu_pct, '%')}</strong>
        <small>Relative to requested CPU, not host CPU</small></div>
      <div className="analytics-stat"><small>Replicas (current → desired)</small>
        <strong>{fmt(infra?.hpa?.current_replicas)} → {fmt(infra?.hpa?.desired_replicas)}</strong>
        <small>Ready Pods: {fmt(infra?.pods?.ready)}</small></div>
      <div className="analytics-stat"><small>Dashboard Pod CPU / memory</small>
        <strong>{fmt(infra?.usage?.cpu_millicores,'m')} / {fmt(infra?.usage?.memory_mib,' MiB')}</strong>
        <small>Measured across dashboard containers</small></div>
      <div className="analytics-stat"><small>Synthetic HTTP throughput</small>
        <strong>{fmt(experiment?.rps_observed,' req/s')}</strong>
        <small>{experiment ? experiment.attempted + ' requests, ' + experiment.errors + ' errors' :
          'No experiment yet'}</small></div>
    </div>
    <div className="analytics-load">
      <h3>Workload controls</h3>
      <p className="description">Fixed local presets. A maximum of 120 seconds per test,
        bounded concurrency, and no custom request targets.</p>
      <label>Duration <select value={duration} onChange={e => setDuration(Number(e.target.value))}
        disabled={busy || running}>
        {[30,60,90,120].map(n=><option key={n} value={n}>{n} seconds</option>)}
      </select></label>
      <div className="analytics-presets">
        {presets.map(p => <button type="button" key={p.id} onClick={() => void control('start',p.id)}
          disabled={busy || running || !snapshot?.load_enabled}>
          <strong>{p.title}</strong><small>{p.evs} EV-equivalents · {p.hz} Hz</small>
          <small>{p.rps} HTTP req/s</small>
        </button>)}
      </div>
      <button className="analytics-stop" type="button" disabled={busy || !running}
        onClick={() => void control('stop')}>Stop experiment</button>
      {snapshot && !snapshot.load_enabled && <p className="warning">Load generation is disabled.</p>}
      {message && <p role="status" className="success">{message}</p>}
    </div>
    <div className="analytics-meta">
      <span>Observed: {sgt(infra?.observed_at)}</span>
      <span>Experiment: {experiment ? experiment.preset + ' · ' + experiment.state : 'None'}</span>
      <span>p95 latency: {fmt(experiment?.p95_latency_ms,' ms')}</span>
      <span>Storage: {snapshot?.storage === 'local-persistent-volume' ? 'Persistent volume' : 'Ephemeral'}</span>
    </div>
    <div className="analytics-charts">
      <Graph title="HPA CPU (%)" samples={samples} maxValue={100}
        measure={s => s.hpa?.current_cpu_pct} target={infra?.hpa?.target_cpu_pct}/>
      <Graph title="Replicas (current)" samples={samples}
        maxValue={Math.max(3,infra?.hpa?.max_replicas ?? 3)}
        measure={s => s.hpa?.current_replicas}/>
      <Graph title="Synthetic HTTP requests / second" samples={samples}
        maxValue={220} measure={(_,i) => observedRps[i]}/>
      <Graph title="Dashboard Pod CPU (millicores)" samples={samples}
        maxValue={100} measure={s=>s.usage?.cpu_millicores}/>
    </div>
    <h3>Recorded experiments</h3>
    <p className="description">CSV contains timestamped CPU and HPA observations, replica counts,
      throughput, errors and latency. Missing Kubernetes measurements are left blank.
      Experiments persist through restarts when the analytics volume is retained.</p>
    <div className="table-wrap"><table><thead><tr>
      <th>Started (SGT)</th><th>Preset</th><th>State</th><th>Requests</th><th>Errors</th><th>Export</th>
    </tr></thead><tbody>
      {records.map(r=><tr key={r.id}><td>{sgt(r.started_at)}</td><td>{r.preset}</td>
        <td>{r.state}</td><td>{r.attempted}</td><td>{r.errors}</td>
        <td><a href={'/api/probe/v1/analytics/runs/' + r.id + '/csv'}>CSV</a></td></tr>)}
    </tbody></table></div>
    {!records.length && <p className="description">No experiments recorded yet.</p>}
  </section>;
}
