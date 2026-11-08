import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DemandSimulationControl } from '../../simulation-console/frontend/src/DemandSimulationControl';

const paused = {
  enabled: false, running: false, tick_count: 0, last_tick_at: null, last_error: null,
  configuration: { update_interval_seconds: 30 }, zones: {},
};
const running = { ...paused, enabled: true, running: true, tick_count: 1 };

afterEach(() => vi.useRealTimers());

describe('demand simulation control', () => {
  it('starts and pauses only after confirmed responses', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      const body = url.endsWith('/start') ? running : paused;
      return new Response(JSON.stringify(body), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    });
    render(<DemandSimulationControl/>);

    expect(await screen.findByText('Paused')).toBeVisible();
    const start = screen.getByRole('button', { name: 'Start demand simulation' });
    const pause = screen.getByRole('button', { name: 'Pause demand simulation' });
    expect(start).toBeEnabled();
    expect(pause).toBeDisabled();

    fireEvent.click(start);
    await screen.findByText('Running');
    expect(fetch).toHaveBeenCalledWith('/api/probe/v1/demand-simulation/start',
      expect.objectContaining({ method: 'POST' }));
    expect(start).toBeDisabled();
    expect(pause).toBeEnabled();

    fireEvent.click(pause);
    await screen.findByText('Paused');
    expect(fetch).toHaveBeenCalledWith('/api/probe/v1/demand-simulation/pause',
      expect.objectContaining({ method: 'POST' }));
  });

  it('updates when another operator changes the state', async () => {
    vi.useFakeTimers();
    let reads = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      reads += 1;
      return new Response(JSON.stringify(reads === 1 ? paused : running), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    });
    render(<DemandSimulationControl/>);

    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('Paused')).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText('Running')).toBeVisible();
  });

  it('shows rejected controls without claiming success', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (String(input).endsWith('/status')) {
        return new Response(JSON.stringify(paused), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ detail: 'Demand service unavailable' }), {
        status: 503, headers: { 'Content-Type': 'application/json' },
      });
    });
    render(<DemandSimulationControl/>);

    await screen.findByText('Paused');
    fireEvent.click(screen.getByRole('button', { name: 'Start demand simulation' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(
      'Action rejected: Demand service unavailable'));
    expect(screen.getByText('Paused')).toBeVisible();
  });
});
