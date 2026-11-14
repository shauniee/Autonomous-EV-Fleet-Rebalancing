import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ChargingHandoffs } from '../src/components/ChargingHandoffs';
import { makeFleet } from './fixtures';

function setup(cable = 'AWAITING_CONNECTION', state = 'AWAITING_CONNECTION', fleetFresh = true) {
  const vehicle = makeFleet().vehicles[0];
  vehicle.effective_availability = 'UNAVAILABLE';
  vehicle.telemetry = { ...vehicle.telemetry!, soc_pct: state === 'COMPLETE' ? 80 : 70,
    charging_state: state as 'CHARGING', parked_facility_id: 'charger-1', speed_kph: 0 };
  const parking = { vehicle_id: 'ev-01', pending: false, parking: {
    facility_id: 'charger-1', name: 'Bishan charging hub', charging_allocated: true,
    charging_session_id: 'session-1', charging_target_soc_pct: 80, cable_status: cable,
    cable_connected_by: cable === 'CONNECTED' ? 'Ground Team B' : null,
  } };
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_path, options) =>
    new Response(JSON.stringify(options?.method === 'POST' ? parking : { 'ev-01': parking })));
  const notify = vi.fn();
  const onUpdated = vi.fn();
  const props = { vehicles: [vehicle], fleetFresh, onUpdated, notify };
  render(<ChargingHandoffs {...props} />);
  return { fetch, props, parking };
}

describe('ground-team charging handoff', () => {
  it('explains the waiting stage and records the named team with its allocation', async () => {
    const { fetch, props } = setup();
    expect(await screen.findByText('Awaiting cable connection')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Ground team for cable handoff'), { target: { value: 'Ground Team C' } });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cable connected for ev-01' }));
    await waitFor(() => expect(props.onUpdated).toHaveBeenCalled());
    const post = fetch.mock.calls.find(([, options]) => options?.method === 'POST')!;
    expect(post[0]).toBe('/api/facilities/vehicles/ev-01/charging-cable');
    expect(JSON.parse(String(post[1]?.body))).toMatchObject({
      dispatch_id: 'session-1', action: 'CONNECT', staff_name: 'Ground Team C', request_id: expect.any(String),
    });
  });

  it('keeps charging vehicles unavailable and only offers disconnection at the target', async () => {
    setup('CONNECTED', 'CHARGING');
    expect(await screen.findByText('Charging in progress')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Confirm cable disconnected/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Connected by Ground Team B/)).toBeInTheDocument();
  });

  it('records disconnection when charging is complete', async () => {
    const { fetch } = setup('CONNECTED', 'COMPLETE');
    expect(await screen.findByText('Awaiting cable disconnection')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cable disconnected for ev-01' }));
    await waitFor(() => expect(fetch.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(true));
    const post = fetch.mock.calls.find(([, options]) => options?.method === 'POST')!;
    expect(JSON.parse(String(post[1]?.body)).action).toBe('DISCONNECT');
  });

  it('disables cable confirmations when Fleet observations are unavailable', async () => {
    setup('AWAITING_CONNECTION', 'AWAITING_CONNECTION', false);
    expect(await screen.findByRole('button', { name: 'Confirm cable connected for ev-01' })).toBeDisabled();
    expect(screen.getByText(/Fresh stopped-vehicle telemetry is required/)).toBeInTheDocument();
  });

  it('retries a lost response with the same request and reports recovery inline', async () => {
    const { fetch, props, parking } = setup();
    let failed = false;
    fetch.mockImplementation(async (_path, options) => {
      if (options?.method === 'POST' && !failed) { failed = true; throw new Error('Network lost'); }
      return new Response(JSON.stringify(options?.method === 'POST' ? parking : { 'ev-01': parking }));
    });
    const button = await screen.findByRole('button', { name: 'Confirm cable connected for ev-01' });
    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent(/Retry this confirmation/);
    fireEvent.click(button);
    await waitFor(() => expect(props.onUpdated).toHaveBeenCalled());
    const requests = fetch.mock.calls.filter(([, options]) => options?.method === 'POST');
    expect(requests).toHaveLength(2);
    expect(requests[0][1]?.body).toBe(requests[1][1]?.body);
  });

  it('resolves a lost response when polling confirms the committed cable stage', async () => {
    const { fetch, parking } = setup();
    fetch.mockImplementation(async (_path, options) => {
      if (options?.method === 'POST') {
        parking.parking.cable_status = 'CONNECTED';
        throw new Error('Response lost after commit');
      }
      return new Response(JSON.stringify({ 'ev-01': parking }));
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm cable connected for ev-01' }));
    await screen.findByRole('alert');
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument(), { timeout: 4000 });
    expect(screen.getByText('Cable connection confirmed by charging-bay observations.')).toBeInTheDocument();
  });

  it('shows a recoverable observation error instead of an empty charging queue', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('Offline'));
    render(<ChargingHandoffs vehicles={[]} fleetFresh={false} notify={vi.fn()} onUpdated={vi.fn()} />);
    expect(await screen.findByText(/Charging-bay observations unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/No EVs occupying/)).not.toBeInTheDocument();
  });
});
