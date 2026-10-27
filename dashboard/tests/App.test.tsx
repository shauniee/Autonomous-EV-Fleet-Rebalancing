import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import { makeDispatch, makeFleet, makeHeat, makeRecommendation } from './fixtures';
vi.mock('../src/components/SingaporeMap', () => ({ SingaporeMap: () => <div>Map canvas</div> }));

function server(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = {
    '/api/demand/heat-map': makeHeat(), '/api/fleet/vehicles': makeFleet(),
    '/api/dispatch/dispatches': { dispatches: [] }, '/api/rebalancing/recommendations': makeRecommendation(), ...overrides,
  };
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === '/api/dispatch/dispatches' && init?.method === 'POST') return new Response(JSON.stringify(makeDispatch()));
    const body = responses[path];
    if (body instanceof Error) throw body;
    return new Response(JSON.stringify(body ?? { origin: { latitude: 1.35, longitude: 103.85 } }));
  });
}
async function selectAndRecommend() {
  fireEvent.click(await screen.findByRole('button', { name: /CBD CRITICAL/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Recommend EV' }));
  await screen.findByTestId('recommendation');
}
describe('dispatcher dashboard', () => {
  it('renders all zones using backend severity, independently of high raw demand', async () => {
    server(); render(<App/>);
    expect(await screen.findByRole('button', { name: /Bishan NORMAL/ })).toBeInTheDocument();
    expect(within(screen.getByLabelText('Select target zone')).getAllByRole('button')).toHaveLength(6);
    fireEvent.click(screen.getByRole('button', { name: /CBD CRITICAL/ }));
    expect(screen.getByText('Current shortage')).toBeInTheDocument();
    expect(screen.getByText('Projected shortage')).toBeInTheDocument();
  });
  it('separates recommendation from approval and shows backend explanation', async () => {
    const fetch = server(); render(<App/>); await selectAndRecommend();
    expect(screen.getByText('Backend selected the highest scoring eligible EV.')).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([path, init]) => path === '/api/dispatch/dispatches' && init?.method === 'POST')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Review dispatch' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('bishan')).toBeInTheDocument();
    expect(within(dialog).getByText('85%')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve Dispatch' }));
    await screen.findByText(/Dispatch dispatch-1:/);
    expect(fetch.mock.calls.filter(([path, init]) => path === '/api/dispatch/dispatches' && init?.method === 'POST')).toHaveLength(1);
  });
  it('keeps partial demand visible and null supply unknown', async () => {
    const heat = makeHeat(); heat.status = 'PARTIAL'; heat.fleet_status = 'UNAVAILABLE'; heat.issues = ['FLEET_UNAVAILABLE'];
    heat.zones[0].available_evs = null; heat.zones[0].hotspot_severity = 'UNKNOWN';
    server({ '/api/demand/heat-map': heat, '/api/fleet/vehicles': new Error('Fleet down') }); render(<App/>);
    fireEvent.click(await screen.findByRole('button', { name: /CBD UNKNOWN/ }));
    expect(screen.getByText(/FLEET_UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getAllByText('Unknown').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeDisabled();
  });
  it('keeps fleet visible when Demand is unavailable', async () => {
    server({ '/api/demand/heat-map': new Error('Demand down') }); render(<App/>);
    expect(await screen.findByTestId('vehicle-ev-01')).toHaveTextContent('90%');
    expect(await screen.findByText(/Demand · unavailable/)).toBeInTheDocument();
  });
  it('shows backend relocation, acknowledgement and uncertainty without inventing progress', async () => {
    const dispatch = makeDispatch(); dispatch.state = 'UNKNOWN'; dispatch.failure_reason = 'PROGRESS_TIMEOUT';
    server({ '/api/dispatch/dispatches': { dispatches: [dispatch] } }); render(<App/>);
    const card = await screen.findByTestId('dispatch-dispatch-1');
    expect(card).toHaveTextContent('PROGRESS_TIMEOUT'); expect(card).toHaveTextContent('2 km');
    expect(card).toHaveTextContent('Acknowledged / execution confirmed'); expect(card).toHaveTextContent('retains ownership');
  });
  it('blocks expired recommendation approval', async () => {
    const rec = makeRecommendation(); rec.expires_at = new Date(0).toISOString();
    server({ '/api/rebalancing/recommendations': rec }); render(<App/>); await selectAndRecommend();
    expect(screen.getByRole('button', { name: 'Review dispatch' })).toBeDisabled();
  });
  it('displays backend exclusions when no EV is eligible without allowing approval', async () => {
    const rec = makeRecommendation(); rec.selected_vehicle_id = null;
    rec.excluded_candidates = [{ ...rec.ranked_candidates[0], exclusion_reasons: ['STALE_TELEMETRY'] }];
    rec.ranked_candidates = [];
    server({ '/api/rebalancing/recommendations': rec }); render(<App/>); await selectAndRecommend();
    expect(screen.getByText('No eligible EV')).toBeInTheDocument();
    expect(screen.getByText('ev-01: STALE_TELEMETRY')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Review dispatch' })).toBeDisabled();
  });
  it('shows stale telemetry and disables review while preserving the recommendation explanation', async () => {
    const fleet = makeFleet(); fleet.vehicles[0].stale = true;
    server({ '/api/fleet/vehicles': fleet }); render(<App/>); await selectAndRecommend();
    expect(screen.getByTestId('vehicle-ev-01')).toHaveTextContent('STALE');
    expect(screen.getByRole('button', { name: 'Review dispatch' })).toBeDisabled();
    expect(screen.getByText('Backend selected the highest scoring eligible EV.')).toBeInTheDocument();
  });
  it('clears the recommendation when target changes', async () => {
    server(); render(<App/>); await selectAndRecommend();
    fireEvent.click(screen.getByRole('button', { name: /Bishan NORMAL/ }));
    expect(screen.queryByTestId('recommendation')).not.toBeInTheDocument();
  });
  it('ignores a recommendation that arrives after its target was changed', async () => {
    const fetch = server(); const base = fetch.getMockImplementation()!;
    let finish!: (response: Response) => void;
    fetch.mockImplementation((input, init) => String(input).includes('recommendations') ? new Promise(resolve => { finish = resolve; }) : base(input, init));
    render(<App/>); fireEvent.click(await screen.findByRole('button', { name: /CBD CRITICAL/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Recommend EV' }));
    fireEvent.click(screen.getByRole('button', { name: /Bishan NORMAL/ }));
    finish(new Response(JSON.stringify(makeRecommendation())));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeEnabled());
    expect(screen.queryByTestId('recommendation')).not.toBeInTheDocument();
  });
  it('retries an ambiguous approval with the same ID, also after reload', async () => {
    const fetch = server(); const base = fetch.getMockImplementation()!;
    const bodies: string[] = [];
    fetch.mockImplementation(async (input, init) => {
      if (String(input) === '/api/dispatch/dispatches' && init?.method === 'POST') {
        bodies.push(String(init.body)); throw new Error('Response lost');
      }
      return base(input, init);
    });
    const view = render(<App/>); await selectAndRecommend();
    fireEvent.click(screen.getByRole('button', { name: 'Review dispatch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve Dispatch' }));
    await screen.findByText(/Approval outcome uncertain/);
    view.unmount(); render(<App/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry same approval' }));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[0]).toEqual(bodies[1]);
  });
  it('shows simulated parking/charger capacity for the selected zone without changing dispatch controls', async () => {
    const site = {
      facility_id: 'mock-cbd-01', name: 'CBD simulated parking 1', zone_id: 'cbd',
      position: { latitude: 1.2842, longitude: 103.8532 },
      parking_capacity: 90, parking_available: 12,
      charging_bays: 12, charging_available: 6, connector_types: ['CCS2'],
      status: 'OPEN', source: 'MOCK',
    };
    server({ '/api/facilities/facilities': { data_source: 'MOCK', generated_at: new Date().toISOString(), facilities: [site] } });
    render(<App/>);
    fireEvent.click(await screen.findByRole('button', { name: /CBD CRITICAL/ }));
    const facility = await screen.findByTestId('facility-mock-cbd-01');
    expect(facility).toHaveTextContent('CBD simulated parking 1');
    expect(facility).toHaveTextContent('12 / 90');
    expect(facility).toHaveTextContent('6 / 12');
    expect(screen.getByText(/current EV recommendation does not use these spaces/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeEnabled());
  });
  it('keeps Fleet and Demand controls available during a Facilities outage', async () => {
    server({ '/api/facilities/facilities': new Error('Facilities down') });
    render(<App/>);
    expect(await screen.findByText(/Facilities \(mock\) · unavailable/)).toBeInTheDocument();
    expect(await screen.findByTestId('vehicle-ev-01')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /CBD CRITICAL/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeEnabled());
  });
  it('marks rented EVs and expected end times from Fleet without breaking zone selection', async () => {
    const snapshot = makeFleet();
    snapshot.vehicles[0].effective_availability = 'UNAVAILABLE';
    snapshot.vehicles[0].active_rental = {
      rental_id: 'rental-demo', status: 'RENTED', started_at: new Date().toISOString(),
      expected_end_at: new Date(Date.now() + 3600000).toISOString(),
      return_position: null, return_soc_pct: null,
    };
    server({ '/api/fleet/vehicles': snapshot });
    render(<App/>);
    const row = await screen.findByTestId('vehicle-ev-01');
    expect(row).toHaveTextContent('RENTED');
    expect(row).toHaveTextContent('Expected return');
    expect(row).toHaveTextContent('UNAVAILABLE');
    fireEvent.click(screen.getByRole('button', { name: /CBD CRITICAL/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeEnabled());
  });

});
