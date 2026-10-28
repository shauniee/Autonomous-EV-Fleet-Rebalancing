import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import { makeDispatch, makeFleet, makeHeat, makeRecommendation } from './fixtures';
vi.mock('../src/components/SingaporeMap', () => ({ SingaporeMap: () => <div>Map canvas</div> }));

function server(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = {
    '/api/demand/heat-map': makeHeat(), '/api/fleet/vehicles': makeFleet(),
    '/api/facilities/facilities': { generated_at: new Date().toISOString(), data_source: 'MOCK', facilities: [] },
    '/api/dispatch/dispatches': { dispatches: [] }, '/api/rebalancing/recommendations': makeRecommendation(),
    '/api/rebalancing/traffic/conditions': { source: 'DISABLED', status: 'DISABLED',
      generated_at: new Date().toISOString(), feeds: {}, speeds: [], incidents: [], roadworks: [] },
    '/api/rebalancing/charging-advice': { generated_at: new Date().toISOString(), trigger_soc_pct: 25,
      min_arrival_reserve_pct: 20, vehicles: [], assumptions: [] }, ...overrides,
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
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve 1 EV' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'Approve 1 EV' }));
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
    expect(screen.getByText(/Batch recommendations evaluate and internally reserve simulated spaces/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeEnabled());
  });
  it('keeps observations available but blocks new destinations when the provider is unknown', async () => {
    server({ '/api/facilities/facilities': new Error('Facilities down') });
    render(<App/>);
    expect(await screen.findByText(/Facilities \(mock\) · unavailable/)).toBeInTheDocument();
    expect(await screen.findByTestId('vehicle-ev-01')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /CBD CRITICAL/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeDisabled());
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

  it('previews bulk parking-aware recommendations and requires explicit batch approval', async () => {
    const assignments = [{
      recommendation_id: 'rec-parking-1', vehicle_id: 'ev-01',
      facility_id: 'mock-cbd-01', facility_name: 'CBD simulated parking 1',
      target_position: { latitude: 1.2842, longitude: 103.8532 },
      requires_charging: false, distance_km: 8,
      predicted_arrival_soc_pct: 85, score: .8,
    }];
    const batch = {
      batch_recommendation_id: 'batch-recommendation', target_zone_id: 'cbd',
      requested_count: 3, recommended_count: 1, unfulfilled_count: 2,
      created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 30000).toISOString(),
      explanation: 'Proposed one parking destination', assumptions: ['MOCK'],
      assignments,
    };
    const outcome = {
      request_id: 'req-1', batch_recommendation_id: batch.batch_recommendation_id,
      requested_count: 3, recommended_count: 1, status: 'APPROVED',
      items: [{ recommendation_id: 'rec-parking-1', vehicle_id: 'ev-01',
        facility_id: 'mock-cbd-01', dispatch_id: 'dispatch-11', state: 'PENDING', error: null }],
    };
    const fetch = server({
      '/api/rebalancing/batch-recommendations': batch,
      '/api/dispatch/batches': outcome,
      '/api/facilities/facilities': { generated_at: new Date().toISOString(), data_source: 'MOCK', facilities: [] },
    });
    render(<App/>);
    fireEvent.click(await screen.findByRole('button', { name: /CBD CRITICAL/ }));
    fireEvent.change(screen.getByLabelText('Number of EVs needed'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Recommend 3 EVs' }));
    expect(await screen.findByTestId('batch-recommendation')).toHaveTextContent('1 of 3 EVs recommended');
    expect(screen.getByTestId('batch-recommendation')).toHaveTextContent('CBD simulated parking 1');
    const recommendations = fetch.mock.calls.filter(([path, init]) =>
      path === '/api/rebalancing/batch-recommendations' && init?.method === 'POST');
    expect(JSON.parse(String(recommendations[0][1]!.body))).toEqual({ target_zone_id: 'cbd', quantity: 3 });
    expect(fetch.mock.calls.filter(([path, init]) =>
      path === '/api/dispatch/batches' && init?.method === 'POST')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Review dispatch' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Confirm 1 EV relocation');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve 1 EV' }));
    const result = await screen.findByRole('region', { name: 'Batch approval outcomes' });
    expect(result).toHaveTextContent('APPROVED');
    const submissions = fetch.mock.calls.filter(([path, init]) =>
      path === '/api/dispatch/batches' && init?.method === 'POST');
    expect(submissions).toHaveLength(1);
    expect(JSON.parse(String(submissions[0][1]!.body))).toHaveProperty('batch_recommendation_id', 'batch-recommendation');
  });

  it('shows LTA provenance and unknown totals and uses a facility destination for one EV', async () => {
    const fetchedAt = new Date().toISOString();
    const site = {
      facility_id: 'lta-parking-LTA-A1', name: 'Real-coordinate fixture carpark', zone_id: 'cbd',
      position: { latitude: 1.283, longitude: 103.851 }, source: 'LTA_DATAMALL', status: 'OPEN',
      parking_capacity: null, parking_available: 3, reported_parking_available: 4,
      charging_bays: null, charging_available: 0, connector_types: [],
      availability_observed_at: fetchedAt,
    };
    const fetch = server({
      '/api/facilities/facilities': { generated_at: fetchedAt, data_source: 'LTA_DATAMALL', facilities: [site],
        feeds: [{ name: 'parking', status: 'FRESH', last_success_at: fetchedAt, error_code: null },
          { name: 'charging', status: 'ERROR', last_success_at: null, error_code: 'LTA_AUTH_FAILED' }] },
      '/api/rebalancing/batch-recommendations': {
        batch_recommendation_id: 'batch-lta', target_zone_id: 'cbd', requested_count: 1,
        recommended_count: 1, unfulfilled_count: 0, created_at: fetchedAt,
        expires_at: new Date(Date.now() + 30000).toISOString(), explanation: 'LTA destination', assumptions: [],
        assignments: [{ recommendation_id: 'rec-lta', vehicle_id: 'ev-01', facility_id: site.facility_id,
          facility_name: site.name, target_position: site.position, requires_charging: false,
          distance_km: 8, predicted_arrival_soc_pct: 85, score: .8 }],
      },
    });
    render(<App/>);
    await screen.findByText('LTA DATAMALL · SIMULATED ALLOCATIONS');
    fireEvent.click(await screen.findByRole('button', { name: /CBD CRITICAL/ }));
    const card = await screen.findByTestId('facility-lta-parking-LTA-A1');
    expect(card).toHaveTextContent('3 / total unknown');
    expect(card).toHaveTextContent('Reported by LTA before simulated allocations4');
    expect(card).toHaveTextContent('Charging bays availableUnknown');
    expect(screen.getByText(/LTA charging: ERROR/)).toHaveTextContent('LTA_AUTH_FAILED');
    fireEvent.click(screen.getByRole('button', { name: 'Recommend EV' }));
    await screen.findByTestId('batch-recommendation');
    const requests = fetch.mock.calls.filter(([path, init]) =>
      path === '/api/rebalancing/batch-recommendations' && init?.method === 'POST');
    expect(JSON.parse(String(requests[0][1]!.body))).toEqual({ target_zone_id: 'cbd', quantity: 1 });
    expect(fetch.mock.calls.some(([path, init]) => path === '/api/rebalancing/recommendations' && init?.method === 'POST')).toBe(false);
    expect(fetch.mock.calls.some(([, init]) => JSON.stringify(init ?? {}).includes('AccountKey'))).toBe(false);
  });

  it('shows missing LTA credentials explicitly without claiming mock data is live', async () => {
    server({ '/api/facilities/facilities': { generated_at: new Date().toISOString(),
      data_source: 'LTA_DATAMALL', facilities: [], feeds: [
        { name: 'parking', status: 'MISSING_KEY', last_success_at: null, error_code: null },
        { name: 'charging', status: 'MISSING_KEY', last_success_at: null, error_code: null },
      ] } });
    render(<App/>);
    expect(await screen.findByText(/LTA parking: MISSING_KEY/)).toBeInTheDocument();
    expect(screen.queryByText('MOCK · NOT REAL PARKING')).not.toBeInTheDocument();
  });

  it('prepares exact low-SOC charger route without issuing any dispatch before approval', async () => {
    const fleet = makeFleet(); fleet.vehicles[0].telemetry!.soc_pct = 25;
    const site = {
      facility_id: 'mock-bishan-01', name: 'Bishan simulated parking 1', zone_id: 'bishan',
      position: { latitude: 1.351, longitude: 103.85 }, source: 'MOCK', status: 'OPEN',
      parking_capacity: 66, parking_available: 14, charging_bays: 6, charging_available: 3,
      connector_types: ['CCS2'],
    };
    const now = new Date().toISOString();
    const advice = {
      generated_at: now, trigger_soc_pct: 25, min_arrival_reserve_pct: 20, assumptions: [],
      vehicles: [{ vehicle_id: 'ev-01', current_soc_pct: 25, status: 'RECOMMENDED', reasons: [],
        charger: { facility_id: site.facility_id, facility_name: site.name, zone_id: 'bishan',
          source: 'MOCK', distance_km: .15, energy_required_kwh: .05,
          predicted_arrival_soc_pct: 24.9, charging_available: 3, charging_power_kw: null } }],
    };
    const proposed = {
      batch_recommendation_id: 'safe-charge-batch', target_zone_id: 'bishan', purpose: 'CHARGE',
      requested_count: 1, recommended_count: 1, unfulfilled_count: 0,
      created_at: now, expires_at: new Date(Date.now() + 60000).toISOString(),
      explanation: 'One feasible charging destination', assumptions: [],
      assignments: [{ recommendation_id: 'safe-charge', vehicle_id: 'ev-01',
        facility_id: site.facility_id, facility_name: site.name, target_position: site.position,
        requires_charging: true, distance_km: .15, predicted_arrival_soc_pct: 24.9, score: .7 }],
    };
    const fetch = server({
      '/api/fleet/vehicles': fleet,
      '/api/facilities/facilities': { generated_at: now, data_source: 'MOCK', facilities: [site] },
      '/api/rebalancing/charging-advice': advice,
      '/api/rebalancing/batch-recommendations': proposed,
    });
    render(<App/>);
    const action = await screen.findByRole('button', { name: 'Prepare charger route' });
    await waitFor(() => expect(action).toBeEnabled());
    fireEvent.click(action);
    expect(screen.getByLabelText('Destination purpose')).toHaveValue('CHARGE');
    expect(screen.getByLabelText('Vehicle')).toHaveValue('ev-01');
    expect(screen.getByText(/Charging advisory destination:/)).toHaveTextContent('mock-bishan-01');
    expect(fetch.mock.calls.some(([path, init]) => path === '/api/dispatch/batches' && init?.method === 'POST')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Recommend EV' }));
    expect(await screen.findByTestId('batch-recommendation')).toHaveTextContent('Bishan simulated parking 1');
    const requests = fetch.mock.calls.filter(([path, init]) =>
      path === '/api/rebalancing/batch-recommendations' && init?.method === 'POST');
    expect(JSON.parse(String(requests[0][1]!.body))).toEqual({
      target_zone_id: 'bishan', quantity: 1, purpose: 'CHARGE',
      vehicle_id: 'ev-01', facility_id: 'mock-bishan-01',
    });
    expect(fetch.mock.calls.some(([path, init]) => path === '/api/dispatch/batches' && init?.method === 'POST')).toBe(false);
  });

  it('does not offer a charging route when the backend cannot find a safe charger', async () => {
    const fleet = makeFleet(); fleet.vehicles[0].telemetry!.soc_pct = 19;
    server({ '/api/fleet/vehicles': fleet,
      '/api/rebalancing/charging-advice': {
        generated_at: new Date().toISOString(), trigger_soc_pct: 25, min_arrival_reserve_pct: 20,
        vehicles: [{ vehicle_id: 'ev-01', current_soc_pct: 19,
          status: 'INELIGIBLE', reasons: ['BELOW_OPERATIONAL_RESERVE'], charger: null }],
        assumptions: [],
      },
    });
    render(<App/>);
    expect(await screen.findByText(/BELOW_OPERATIONAL_RESERVE/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Prepare charger route' })).not.toBeInTheDocument();
  });
  it('shows disabled LTA traffic without implying clear roads', async () => {
    server(); render(<App/>);
    const panel = await screen.findByRole('region', { name: 'Traffic conditions and roadworks' });
    expect(within(panel).getByText(/Live LTA traffic ranking is disabled/)).toBeInTheDocument();
    expect(within(panel).getByText('DISABLED')).toBeInTheDocument();
  });

  it('shows traffic feed freshness and geolocated incident/approved roadworks', async () => {
    server({ '/api/rebalancing/traffic/conditions': {
      source: 'LTA_DATAMALL', status: 'FRESH', generated_at: new Date().toISOString(),
      feeds: { speeds: {status:'FRESH', observed_at: new Date().toISOString(), count:1, error:null},
        incidents: {status:'FRESH', observed_at: new Date().toISOString(), count:1, error:null},
        roadworks: {status:'FRESH', observed_at: new Date().toISOString(), count:1, error:null} },
      speeds: [{ id:'speed-1', road:'FIRST ROAD', start:{ latitude:1.3,longitude:103.8 },
        end:{ latitude:1.3,longitude:103.81 }, min_kph:10,max_kph:19 }],
      incidents: [{ id:'incident-1', type:'ROAD BLOCK',
        position:{ latitude:1.3,longitude:103.8 }, message:'Synthetic fixture closure' }],
      roadworks: [{ id:'work-1', road:'FIRST ROAD',
        start_date:'2020-01-01', end_date:'2099-01-01' }],
    }});
    render(<App/>);
    const panel = await screen.findByRole('region', { name:'Traffic conditions and roadworks' });
    expect(within(panel).getAllByText('FRESH').length).toBeGreaterThan(0);
    expect(within(panel).getByText('Synthetic fixture closure')).toBeInTheDocument();
    expect(within(panel).getByText('FIRST ROAD')).toBeInTheDocument();
  });

  it('shows read-only latest digital twin diagnostics and accepted history', async () => {
    const fleet = makeFleet();
    fleet.vehicles[0].telemetry!.speed_kph = 28;
    fleet.vehicles[0].telemetry!.battery_temp_c = 32;
    fleet.vehicles[0].telemetry!.gps_quality = 'SIMULATED';
    const now = new Date().toISOString();
    const history = { vehicle_id: 'ev-01', storage: 'postgresql', limit: 100,
      samples: [
        { session_id: 'test-session', sequence: 1, observed_at: now, received_at: now,
          telemetry: { ...fleet.vehicles[0].telemetry!, soc_pct: 90, speed_kph: 0 } },
        { session_id: 'test-session', sequence: 2, observed_at: now, received_at: now,
          telemetry: { ...fleet.vehicles[0].telemetry!, soc_pct: 89, speed_kph: 28 } },
      ] };
    const fetch = server({ '/api/fleet/vehicles': fleet,
      '/api/fleet/vehicles/ev-01/telemetry-history?limit=100': history });
    render(<App/>);
    const panel = await screen.findByRole('region', { name: 'EV digital twin diagnostics' });
    expect(within(panel).getByText('28 km/h')).toBeInTheDocument();
    expect(within(panel).getByText('32°C')).toBeInTheDocument();
    expect(within(panel).getByText('SIMULATED')).toBeInTheDocument();
    expect(await within(panel).findByText(/2 accepted samples shown/)).toBeInTheDocument();
    expect(within(panel).getByRole('img', { name: 'Historical battery state of charge' })).toBeInTheDocument();
    expect(fetch.mock.calls.some(([path, init]) =>
      String(path).includes('telemetry-history') && init?.method === 'POST')).toBe(false);
  });

});
