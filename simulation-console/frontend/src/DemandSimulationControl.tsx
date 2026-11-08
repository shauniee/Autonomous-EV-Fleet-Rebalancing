import { useEffect, useState } from 'react';

type Status = {
  enabled: boolean;
  running: boolean;
  tick_count: number;
  last_tick_at: string | null;
  last_error: string | null;
  configuration: { update_interval_seconds?: number };
};

export function DemandSimulationControl() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<'start' | 'pause' | null>(null);
  const [notice, setNotice] = useState('');

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      try {
        const response = await fetch('/api/probe/v1/demand-simulation/status', {
          signal: AbortSignal.timeout(5000),
        });
        if (!response.ok) throw new Error('Demand simulation controller is unavailable');
        const next: Status = await response.json();
        if (!stopped) { setStatus(next); setError(''); }
      } catch (caught) {
        if (!stopped) {
          setStatus(null);
          setError(caught instanceof Error ? caught.message : 'Controller unavailable');
        }
      }
      if (!stopped) timer = setTimeout(poll, 5000);
    }

    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, []);

  async function control(action: 'start' | 'pause') {
    setBusy(action);
    setNotice('');
    try {
      const response = await fetch(`/api/probe/v1/demand-simulation/${action}`, {
        method: 'POST', signal: AbortSignal.timeout(8000),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? `HTTP ${response.status}`);
      }
      const next: Status = await response.json();
      setStatus(next);
      setError('');
      setNotice(action === 'start'
        ? 'Demand simulation started. Zone demand will update on the configured interval.'
        : 'Demand simulation paused. Existing demand observations were preserved.');
    } catch (caught) {
      setNotice(`Action rejected: ${caught instanceof Error ? caught.message : 'Unknown error'}`);
    } finally {
      setBusy(null);
    }
  }

  const state = error ? 'Unavailable' : busy === 'start' ? 'Starting' : busy === 'pause' ? 'Pausing'
    : status?.running ? 'Running' : status ? 'Paused' : 'Checking…';

  return <section aria-label="Demand simulation controller">
    <h2>Demand simulation</h2>
    <p className="description">Zone demand stays fixed while paused. Starting generates one fresh snapshot immediately, then updates it on the configured interval.</p>
    <p role="status">Demand generator: <strong>{state}</strong>
      {status?.running && typeof status.configuration.update_interval_seconds === 'number'
        ? ` · every ${status.configuration.update_interval_seconds} seconds` : ''}</p>
    {error && <p className="warning" role="alert">{error}</p>}
    <div className="button-row">
      <button className="primary-action" disabled={!!busy || !status || status.running}
        onClick={() => void control('start')}>Start demand simulation</button>
      <button disabled={!!busy || !status || !status.running}
        onClick={() => void control('pause')}>Pause demand simulation</button>
    </div>
    {notice && <p role={notice.startsWith('Action rejected:') ? 'alert' : 'status'}>{notice}</p>}
  </section>;
}
