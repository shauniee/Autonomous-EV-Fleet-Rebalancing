import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnalyticsPanel } from '../../simulation-console/frontend/src/AnalyticsPanel';

const snapshot = {
  infrastructure: {
    observed_at: '2026-11-06T05:19:52Z',
    hpa: { current_cpu_pct: 200, target_cpu_pct: 60, current_replicas: 1,
      desired_replicas: 3, max_replicas: 3 },
    pods: { total: 3, ready: 3 },
    usage: { cpu_millicores: 21.27, memory_mib: 11.04 },
    errors: {},
  },
  experiment: {
    id: 'e6bf5a8d4b51', preset: 'EXTREME', state: 'COMPLETED',
    started_at: '2026-11-06T05:18:56Z', duration_seconds: 60,
    simulated_evs: 100, telemetry_hz: 2, requests_per_second: 200,
    attempted: 4856, success: 4856, errors: 0, p95_latency_ms: 390.9,
    rps_observed: 80.6,
    samples: [
      { timestamp: '2026-11-06T05:19:40Z',
        hpa: { current_cpu_pct: 50, target_cpu_pct: 60, current_replicas: 1,
          desired_replicas: 1, max_replicas: 3 },
        pods: { total: 1, ready: 1 }, usage: { cpu_millicores: 19.83, memory_mib: 11.34 },
        attempted: 3918, success: 3918, errors: 0, p95_latency_ms: 287.5 },
      { timestamp: '2026-11-06T05:19:52Z',
        hpa: { current_cpu_pct: 200, target_cpu_pct: 60, current_replicas: 1,
          desired_replicas: 3, max_replicas: 3 },
        pods: { total: 3, ready: 3 }, usage: { cpu_millicores: 21.27, memory_mib: 11.04 },
        attempted: 4603, success: 4603, errors: 0, p95_latency_ms: 314.3 },
    ],
  },
  load_enabled: true,
  storage: 'local-persistent-volume',
};

afterEach(() => vi.restoreAllMocks());

function serveSnapshot() {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = String(input);
    return new Response(JSON.stringify(url.endsWith('/runs') ? { runs: [] } : snapshot), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  });
}

describe('simulation analytics observations', () => {
  it('shows observed Pods and their measured scaling event instead of lagging HPA current replicas', async () => {
    serveSnapshot();
    render(<AnalyticsPanel/>);

    await waitFor(() => expect(screen.getByText('3 → 3')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Charts' }));
    expect(screen.getByRole('img', { name: 'Dashboard Pods (observed) history' })).toBeInTheDocument();
    expect(screen.getByText(/Dashboard Pods 1 → 3/)).toBeInTheDocument();
  });

  it('expands the HPA CPU chart to include measurements above 100 percent', async () => {
    serveSnapshot();
    render(<AnalyticsPanel/>);

    await screen.findByText('Workload controls');
    fireEvent.click(screen.getByRole('button', { name: 'Charts' }));
    const chart = screen.getByRole('img', { name: 'HPA CPU (%) history' });
    expect(within(chart).getByText('200')).toBeInTheDocument();
  });

  it('keeps workload, charts and run history in exclusive views with working controls', async () => {
    serveSnapshot();
    render(<AnalyticsPanel/>);

    const workload = await screen.findByText('Workload controls');
    const chart = screen.getByRole('img', { name: 'HPA CPU (%) history', hidden: true });
    const history = screen.getByText('Recorded experiments');
    expect(workload).toBeVisible();
    expect(chart).not.toBeVisible();
    expect(history).not.toBeVisible();
    expect(screen.getByRole('button', { name: 'Stop experiment' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Charts' }));
    expect(workload).not.toBeVisible();
    expect(chart).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Runs' }));
    expect(chart).not.toBeVisible();
    expect(history).toBeVisible();
  });
});
