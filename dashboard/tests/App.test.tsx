import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { Zone } from '../src/api/contracts';
import { makeDispatch, makeFleet, makeHeat, makeRecommendation } from './fixtures';
vi.mock('../src/components/SingaporeMap', () => ({
  SingaporeMap: ({ zones, onSelect }: { zones: Zone[]; onSelect: (id: string) => void }) => <div>
    {zones.map(zone => <button key={zone.zone_id} onClick={() => onSelect(zone.zone_id)}>Map marker for {zone.name}</button>)}
  </div>,
}));

function server(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = {
    '/api/demand/heat-map': makeHeat(), '/api/fleet/vehicles': makeFleet(),
    '/api/facilities/facilities': { generated_at: new Date().toISOString(), data_source: 'MOCK', facilities: [] },
    '/api/dispatch/dispatches': { dispatches: [] },
    '/api/dispatch/routing/status': { provider: 'ONEMAP', configured: true, reason_code: null },
    '/api/rebalancing/recommendations': makeRecommendation(),
    '/api/rebalancing/recommendation-requests?offset=0&limit=30': { enabled: false, crds_mode: 'DISABLED', requests: [], next_offset: null },
    '/api/dispatch/dispatches/dispatch-1/recovery': {
      dispatch_id: 'dispatch-1', status: 'BLOCKED', reason_code: 'CHECKPOINT_UNAVAILABLE', can_resume: false,
      operation: null, blockers: [], evidence: { checkpoint_source: 'NONE', checkpoint_at: null },
    },
    '/api/rebalancing/traffic/conditions': { source: 'DISABLED', status: 'DISABLED',
      generated_at: new Date().toISOString(), feeds: {}, speeds: [], incidents: [], roadworks: [] },
    '/api/rebalancing/charging-advice': { generated_at: new Date().toISOString(), trigger_soc_pct: 25,
      min_arrival_reserve_pct: 20, vehicles: [], assumptions: [] }, ...overrides,
  };
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input);
    if (path.startsWith('/api/dispatch/batch-approval-groups?')) {
      return Response.json({ enabled: true, groups: [], next_offset: null });
    }
    if (path === '/api/dispatch/dispatches' && init?.method === 'POST') return new Response(JSON.stringify(makeDispatch()));
    const body = responses[path === '/api/fleet/dashboard/vehicles' ? '/api/fleet/vehicles' : path];
    if (body instanceof Error) throw body;
    return new Response(JSON.stringify(body ?? { origin: { latitude: 1.35, longitude: 103.85 } }));
  });
}
async function selectAndRecommend() {
  fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Recommend EV' }));
  await screen.findByTestId('recommendation');
}
function openNotifications() {
  fireEvent.click(screen.getByRole('button', { name: /^Notifications/ }));
  return within(screen.getByRole('region', { name: 'Notifications' }));
}
describe('dispatcher dashboard', () => {
  it('queues a map request, reviews its plan, and reloads the approval refusal from Dispatch', async () => {
    const path = '/api/rebalancing/recommendation-requests';
    const expires = new Date(Date.now() + 60000).toISOString();
    const batch = {
      batch_recommendation_id: 'queued-batch', target_zone_id: 'cbd',
      requested_count: 1, recommended_count: 1, unfulfilled_count: 0,
      created_at: new Date().toISOString(), expires_at: expires, explanation: 'One EV', assumptions: [],
      assignments: [{
        recommendation_id: 'rec-1', vehicle_id: 'ev-01', facility_id: 'cbd-1',
        facility_name: 'CBD parking', requires_charging: false, distance_km: 4, predicted_arrival_soc_pct: 72,
      }],
    };
    let saved: Record<string, unknown> | undefined;
    let rejected = false;
    let lostResponse = true;
    const drafts: string[] = [];
    const fleet = makeFleet();
    fleet.vehicles.push({ ...fleet.vehicles[0], vehicle_id: 'ev-02' },
      { ...fleet.vehicles[0], vehicle_id: 'ev-03', stale: true });
    const fetch = server({ '/api/fleet/vehicles': fleet });
    const baseline = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === path && init?.method === 'POST') {
        const draft = JSON.parse(String(init.body));
        drafts.push(draft.request_id);
        expect(draft).toMatchObject({ quantity: 2, vehicle_ids: ['ev-01', 'ev-02'] });
        saved = { ...draft, status: 'READY', batch_recommendation_id: batch.batch_recommendation_id,
          approval_request_id: 'stable-approval', submitted_at: batch.created_at,
          recommended_count: 1, expires_at: expires, assignments: batch.assignments, batch: null };
        if (lostResponse) { lostResponse = false; throw new Error('Lost response'); }
        return Response.json(saved, { status: 202 });
      }
      if (url.startsWith(`${path}?`)) return Response.json({
        enabled: true, crds_mode: 'ACTIVE', next_offset: null, requests: saved ? [saved] : [],
      });
      if (url.startsWith(`${path}/`)) return Response.json({ ...saved, batch });
      if (url === '/api/dispatch/batches' && init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toEqual({
          request_id: 'stable-approval', batch_recommendation_id: 'queued-batch',
        });
        rejected = true;
        return Response.json({ error: { code: 'BATCH_EXPIRED' } }, { status: 409 });
      }
      if (url.startsWith('/api/dispatch/batches?')) return Response.json({
        batches: [], refusals: rejected ? [{
          request_id: 'stable-approval', batch_recommendation_id: 'queued-batch', reason_code: 'BATCH_EXPIRED',
        }] : [],
      });
      return baseline(input, init);
    });
    const view = render(<App/>);
    fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
    fireEvent.change(screen.getByLabelText('Vehicle selection'), { target: { value: 'specific' } });
    expect(screen.getByRole('button', { name: 'Request recommendation' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /ev-03.*Telemetry stale/ })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /ev-02/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /ev-01/ }));
    expect(screen.getByLabelText('Number of EVs needed')).toHaveValue(2);
    fireEvent.click(screen.getByRole('button', { name: 'Request recommendation' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
    fireEvent.click(await screen.findByRole('button', { name: 'View request' }));
    expect(drafts).toHaveLength(2);
    expect(drafts[0]).toBe(drafts[1]);
    const review = await screen.findByRole('button', { name: 'Review approval' });
    await waitFor(() => expect(review).toBeEnabled());
    expect(await screen.findByText(/CBD parking · parking/)).toBeVisible();
    fireEvent.click(review);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve 1 EV' }));
    expect(await screen.findByText('Approval rejected', { exact: true })).toBeVisible();
    view.unmount();
    render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: 'Recommendations' }));
    expect(await screen.findByText('Approval rejected', { exact: true })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /Review recommendation for CBD/ }));
    expect(await screen.findByText(/The batch recommendation expired before approval/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Request again' }));
    expect(screen.getByRole('checkbox', { name: /ev-01/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /ev-02/ })).toBeChecked();
    expect(drafts).toHaveLength(2);
  });

  it('blocks overlapping plans and retries a lost bulk submission under the original group ID after reload', async () => {
    const created = new Date().toISOString();
    const expires = new Date(Date.now() + 60000).toISOString();
    const rows = [
      { id: 'cbd', ev: 'ev-01', parking: 'CBD parking' },
      { id: 'bishan', ev: 'ev-01', parking: 'Bishan parking' },
      { id: 'punggol', ev: 'ev-02', parking: 'Punggol parking' },
      { id: 'tampines', ev: 'ev-03', parking: 'Tampines parking' },
    ].map(row => ({
      request_id: row.id, target_zone_id: row.id, submitted_at: created, quantity: 1, recommended_count: 1,
      expires_at: row.id === 'tampines' ? new Date(Date.now() + 4000).toISOString() : expires, status: 'READY', batch_recommendation_id: `batch-${row.id}`,
      approval_request_id: `approval-${row.id}`,
      assignments: [{ vehicle_id: row.ev, facility_name: row.parking, facility_id: row.id }],
    }));
    const attempts: { group_id: string; batch_recommendation_ids: string[] }[] = [];
    let saved: unknown;
    const path = '/api/dispatch/batch-approval-groups';
    const fetch = server({
      '/api/rebalancing/recommendation-requests?offset=0&limit=30': {
        enabled: true, crds_mode: 'ACTIVE', requests: rows, next_offset: null,
      },
    });
    const baseline = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.startsWith('/api/dispatch/batches?')) return Response.json({ batches: [], refusals: [] });
      if (url.startsWith(`${path}?`)) return Response.json({ enabled: true, groups: saved ? [saved] : [], next_offset: null });
      if (url === path && init?.method === 'POST') {
        const payload = JSON.parse(String(init.body));
        attempts.push(payload);
        if (attempts.length === 1) throw new Error('Lost response');
        saved = { group_id: payload.group_id, status: 'QUEUED', created_at: created, updated_at: created,
          reason_code: null, conflicts: [], plans: payload.batch_recommendation_ids.map((id: string) => ({
            batch_recommendation_id: id, status: 'QUEUED', reason_code: null, approval: null,
          })) };
        return Response.json(saved, { status: 202 });
      }
      return baseline(input, init);
    });
    const view = render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Recommendations/ }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select plan for CBD' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select plan for Bishan' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select plan for Punggol' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select plan for Tampines' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected' }));
    const review = within(screen.getByRole('dialog', { name: 'Approve selected plans' }));
    expect(review.getAllByText('Shared EVs ev-01')).toHaveLength(2);
    expect(review.getByRole('button', { name: 'Confirm 4 plans' })).toBeDisabled();
    expect(attempts).toHaveLength(0);
    fireEvent.click(review.getByRole('button', { name: 'Remove Bishan plan' }));
    const actualNow = Date.now.bind(Date);
    const shiftedClock = vi.spyOn(Date, 'now').mockImplementation(() => actualNow() + 4500);
    expect(await review.findByText('Expired', { exact: true })).toBeVisible();
    expect(review.getByRole('button', { name: 'Confirm 3 plans' })).toBeDisabled();
    fireEvent.click(review.getByRole('button', { name: 'Remove Tampines plan' }));
    expect(review.getByText('ev-01 → CBD parking')).toBeVisible();
    expect(review.getByText('ev-02 → Punggol parking')).toBeVisible();
    fireEvent.click(review.getByRole('button', { name: 'Confirm 2 plans' }));
    expect(await screen.findByText(/Dispatch did not confirm receipt/)).toBeVisible();
    shiftedClock.mockRestore();
    view.unmount();

    const reloaded = render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Recommendations/ }));
    const retry = await screen.findByRole('button', { name: 'Retry same group' });
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);
    expect(await screen.findAllByText('Approval queued')).toHaveLength(2);
    expect(attempts).toHaveLength(2);
    expect(attempts[0].batch_recommendation_ids).toEqual(['batch-cbd', 'batch-punggol']);
    expect(attempts[1]).toEqual(attempts[0]);

    reloaded.unmount();
    render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Recommendations/ }));
    expect(await screen.findAllByText('Approval queued')).toHaveLength(2);
    expect(screen.queryByRole('checkbox', { name: 'Select plan for CBD' })).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Select plan for Bishan' })).toBeEnabled();
  });

  it('explains when local routing prevents a real relocation instead of silently offering approval', async () => {
    const fetch = server({
      '/api/dispatch/routing/status': {
        provider: 'SIMULATED', configured: false, reason_code: 'ONEMAP_ROUTE_REQUIRED',
      },
    });
    render(<App/>);
    const status = await screen.findByRole('status', { name: 'Road routing setup' });
    expect(status).toHaveTextContent('Road routing is not configured for dispatch.');
    expect(status).toHaveTextContent('ROUTING_PROVIDER=ONEMAP');
    expect(status).toHaveTextContent('ONEMAP_ACCESS_TOKEN');
    fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
    expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeEnabled();
    expect(fetch.mock.calls.filter(([path, init]) =>
      path === '/api/dispatch/dispatches' && init?.method === 'POST')).toHaveLength(0);
  });

  it('does not claim routing is unavailable when the backend reports a configured provider', async () => {
    server(); render(<App/>);
    await screen.findByRole('button', { name: /CBD Critical/ });
    expect(screen.queryByRole('status', { name: 'Road routing setup' })).not.toBeInTheDocument();
  });

  it('requires explicit confirmation to end a relocation and sends the reviewed revision', async () => {
    const path = '/api/dispatch/dispatches/dispatch-1/resolution';
    const fetch = server({
      '/api/dispatch/dispatches': { dispatches: [{ ...makeDispatch(), state: 'FAILED' }] },
      [path]: { assessment_revision: 'reviewed-state', can_end: true, blocker: null, operation: null,
        stop_position: { latitude: 1.32, longitude: 103.85 }, stop_soc_pct: 64 },
    });
    const stops = () => fetch.mock.calls.filter(([url, init]) => url === path && init?.method === 'POST');
    render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Relocations/ }));
    const end = await screen.findByRole('button', { name: 'End relocation' });
    await waitFor(() => expect(end).toBeEnabled());
    fireEvent.click(end);
    const review = within(screen.getByRole('dialog', { name: 'End relocation' }));
    expect(review.getByText(/Battery 64.0%/)).toBeVisible();
    fireEvent.click(review.getByRole('button', { name: 'Leave open' }));
    expect(stops()).toHaveLength(0);
    fireEvent.click(end);
    const confirm = within(screen.getByRole('dialog', { name: 'End relocation' }));
    const button = confirm.getByRole('button', { name: 'Confirm end relocation' });
    expect(button).toBeDisabled();
    fireEvent.change(confirm.getByRole('textbox'), { target: { value: 'Execution stopped' } });
    fireEvent.click(button);
    expect(stops()).toHaveLength(1);
    expect(JSON.parse(String(stops()[0][1]?.body))).toMatchObject({
      expected_assessment_revision: 'reviewed-state', reason: 'Execution stopped', request_id: expect.any(String),
    });
  });

  it('shows one Low demand badge per zone when the configured Low range includes 10 EVs', async () => {
    const heat = makeHeat();
    for (const zone of heat.zones) zone.hotspot_severity = 'CRITICAL';
    server({ '/api/demand/heat-map': heat });
    render(<App/>);
    await screen.findByRole('button', { name: /Bishan Critical/ });
    fireEvent.click(screen.getByRole('button', { name: 'Edit thresholds' }));
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Moderate starts at' }), { target: { value: '11' } });
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Critical starts at' }), { target: { value: '20' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply thresholds' }));

    const zones = within(screen.getByRole('group', { name: 'Select target zone' }));
    expect(zones.getAllByText('Low')).toHaveLength(6);
    expect(zones.queryByText(/^critical$/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeInTheDocument();
    fireEvent.click(zones.getByRole('button', { name: /CBD Low/ }));
    expect(screen.getByText('Current shortage')).toBeInTheDocument();
    expect(screen.getByText('Projected shortage')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Demand details' }));
    expect(within(screen.getByRole('complementary', { name: 'Demand & supply' })).getByText('Low')).toBeVisible();
  });

  it('marks expired demand and opens a filtered list of idle EVs without parking', async () => {
    const heat = makeHeat();
    heat.available_outside_zones = 1;
    heat.zones[0].demand_status = 'STALE';
    heat.zones[0].demand_observed_at = new Date(Date.now() - 301000).toISOString();
    const fleet = makeFleet();
    const unparked = fleet.vehicles[0];
    fleet.vehicles.push({ ...unparked, vehicle_id: 'ev-parked',
      telemetry: { ...unparked.telemetry!, parked_facility_id: 'parking-1' } });
    server({ '/api/demand/heat-map': heat, '/api/fleet/vehicles': fleet });
    render(<App/>);
    fireEvent.click(await screen.findByRole('button', { name: /CBD Unknown/ }));
    const demand = within(screen.getByRole('region', { name: 'Selected zone demand' }));
    expect(demand.getByText(/Demand expired.*Last reported 10.*SGT/)).toBeVisible();
    expect(demand.queryByText('10', { exact: true })).toBeNull();
    expect(demand.getByText('0', { exact: true })).toBeVisible();
    expect(screen.getByText(/Available outside zones/)).toHaveTextContent('1');
    fireEvent.click(screen.getByRole('button', { name: '1 idle EV without parking' }));
    const table = within(screen.getByRole('region', { name: 'Fleet vehicle table' }));
    expect(table.getByText('ev-01')).toBeVisible();
    expect(table.queryByText('ev-parked')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show all EVs' }));
    expect(table.getByText('ev-parked')).toBeVisible();
  });

  it('collects configured demand level changes in Notifications without emitting a toast', async () => {
    const heat = makeHeat();
    heat.zones[1].rental_demand = 3;
    server({ '/api/demand/heat-map': heat });
    render(<App/>);
    await screen.findByRole('button', { name: /Bishan Low/ });
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeInTheDocument();

    heat.zones[1].rental_demand = 10;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh now' }));
    await screen.findByRole('button', { name: 'Notifications, 1 to review' });

    const notifications = openNotifications();
    expect(notifications.getByText('Bishan rental demand changed from Low to Critical.')).toBeVisible();
    fireEvent.click(notifications.getByRole('button', { name: 'Close' }));
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeInTheDocument();
    expect(openNotifications().getByText(/Bishan rental demand changed/)).toBeVisible();
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
    await openNotifications().findByText(/ev-01 · Approved/);
    expect(fetch.mock.calls.filter(([path, init]) => path === '/api/dispatch/dispatches' && init?.method === 'POST')).toHaveLength(1);
  });
  it('does not report HTTP 202 with a rejected dispatch as approved', async () => {
    const rejected = makeDispatch();
    rejected.state = 'REJECTED';
    rejected.claim_held = false;
    rejected.acknowledged_at = null;
    rejected.failure_reason = 'ONEMAP_ROUTE_REQUIRED';
    const fetch = server();
    const base = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (input, init) =>
      String(input) === '/api/dispatch/dispatches' && init?.method === 'POST'
        ? new Response(JSON.stringify(rejected), { status: 202 })
        : base(input, init));
    render(<App/>);
    await selectAndRecommend();
    fireEvent.click(screen.getByRole('button', { name: 'Review dispatch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 1 EV' }));
    const notifications = openNotifications();
    expect(await notifications.findByText(/could not verify a usable road route/)).toBeVisible();
    expect(notifications.queryByText(/ONEMAP_ROUTE_REQUIRED/)).not.toBeInTheDocument();
    expect(notifications.getByText('Error')).toBeVisible();
    expect(screen.queryByText(/approval recorded/i)).not.toBeInTheDocument();
  });

  it('keeps the same approval pending while HTTP 202 reports UNKNOWN', async () => {
    const uncertain = { ...makeDispatch(), approval_status: 'UNKNOWN', claim_held: null };
    uncertain.state = 'UNKNOWN';
    uncertain.failure_reason = 'CLAIM_UNCONFIRMED';
    const fetch = server();
    const base = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (input, init) =>
      String(input) === '/api/dispatch/dispatches' && init?.method === 'POST'
        ? new Response(JSON.stringify(uncertain), { status: 202 })
        : base(input, init));
    render(<App/>);
    await selectAndRecommend();
    fireEvent.click(screen.getByRole('button', { name: 'Review dispatch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 1 EV' }));
    const notifications = openNotifications();
    expect(await notifications.findByText(/Approval is still unconfirmed/)).toBeVisible();
    expect(notifications.getByText('Unconfirmed approval')).toBeVisible();
    fireEvent.click(notifications.getByRole('button', { name: 'Clear activity' }));
    expect(notifications.getByText('Unconfirmed approval')).toBeVisible();
    expect(notifications.queryByText(/Approval is still unconfirmed/)).not.toBeInTheDocument();
    fireEvent.click(notifications.getByRole('button', { name: 'Close' }));
    expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeEnabled();
    expect(screen.queryByText(/approval recorded/i)).not.toBeInTheDocument();
  });

  it('keeps partial demand visible and null supply unknown', async () => {
    const heat = makeHeat(); heat.status = 'PARTIAL'; heat.fleet_status = 'UNAVAILABLE'; heat.issues = ['FLEET_UNAVAILABLE'];
    heat.zones[0].available_evs = null; heat.zones[0].hotspot_severity = 'UNKNOWN';
    server({ '/api/demand/heat-map': heat, '/api/fleet/vehicles': new Error('Fleet down') }); render(<App/>);
    fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
    expect(await openNotifications().findByText(/FLEET_UNAVAILABLE/)).toBeVisible();
    expect(screen.getAllByText('Unknown').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeDisabled();
  });
  it('keeps fleet visible when Demand is unavailable', async () => {
    server({ '/api/demand/heat-map': new Error('Demand down') }); render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: 'Fleet' }));
    expect(await screen.findByTestId('vehicle-ev-01')).toHaveTextContent('90%');
    expect(await screen.findByText(/Demand · unavailable/)).toBeInTheDocument();
  });
  it('shows backend relocation, acknowledgement and uncertainty without inventing progress', async () => {
    const dispatch = makeDispatch(); dispatch.state = 'UNKNOWN'; dispatch.failure_reason = 'PROGRESS_TIMEOUT';
    server({ '/api/dispatch/dispatches': { dispatches: [dispatch] } }); render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Relocations/ }));
    const card = await screen.findByTestId('dispatch-dispatch-1');
    expect(card).toHaveTextContent('Vehicle updates stopped. Check the relocation status.');
    expect(card).not.toHaveTextContent('PROGRESS_TIMEOUT');
    expect(card).toHaveTextContent('2 km');
    expect(card).toHaveTextContent('Move confirmed');
    expect(card).toHaveTextContent(/Claim held\s*Yes/);
  });
  it('paginates relocations and combines search with request time and outcome filters', async () => {
    const dispatches = Array.from({ length: 8 }, (_, index) => ({
      ...makeDispatch(),
      dispatch_id: `dispatch-${index}`,
      vehicle_id: `ev-0${index}`,
      created_at: index === 0 ? '2026-11-06T17:00:00Z' : '2026-11-07T06:00:00Z',
      state: index === 0 ? 'SUCCEEDED' : 'IN_PROGRESS',
      claim_held: index !== 0,
      revalidated_metrics: {
        origin: { position: null, zone_id: index === 0 ? 'jurong-east' : 'bishan', facility_id: null },
      },
    }));
    server({ '/api/dispatch/dispatches': { dispatches } });
    render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Relocations/ }));
    await screen.findByText('Showing 1–6 of 8');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByText('Showing 7–8 of 8')).toBeVisible();

    const search = screen.getByRole('searchbox', { name: 'Search relocations' });
    fireEvent.change(search, { target: { value: 'EV-00 Jurong East CBD' } });
    expect(screen.getByText('Showing 1–1 of 1')).toBeVisible();
    expect(screen.getByTestId('dispatch-dispatch-0')).toHaveTextContent('Jurong East');

    fireEvent.change(screen.getByLabelText('Request date (SGT)'), { target: { value: '2026-11-07' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Time' }));
    const timeFilter = screen.getByLabelText('Request time (SGT)');
    fireEvent.change(timeFilter, { target: { value: '12' } });
    expect(screen.getByText(/No relocations match/)).toBeVisible();
    fireEvent.change(timeFilter, { target: { value: '0' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Outcome' }));
    fireEvent.change(screen.getByLabelText('Relocation outcome'), { target: { value: 'SUCCEEDED' } });
    expect(screen.getByTestId('dispatch-dispatch-0')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Clear search and filters' }));
    expect(search).toHaveValue('');
    expect(screen.getByText('Showing 1–6 of 8')).toBeVisible();
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
    expect(screen.getByText('No eligible EV')).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
    expect(screen.getByText('ev-01: STALE_TELEMETRY')).toBeVisible();
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
    fireEvent.click(screen.getByRole('button', { name: 'Map marker for Bishan' }));
    expect(screen.queryByTestId('recommendation')).not.toBeInTheDocument();
  });
  it('ignores a recommendation that arrives after its target was changed', async () => {
    const fetch = server(); const base = fetch.getMockImplementation()!;
    let finish!: (response: Response) => void;
    fetch.mockImplementation((input, init) => String(input).includes('recommendations') ? new Promise(resolve => { finish = resolve; }) : base(input, init));
    render(<App/>); fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Recommend EV' }));
    fireEvent.click(screen.getByRole('button', { name: /Bishan Critical/ }));
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
    await openNotifications().findByText(/Approval is still unconfirmed/);
    view.unmount(); render(<App/>);
    fireEvent.click(openNotifications().getByRole('button', { name: 'Retry same approval' }));
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
    fireEvent.click(screen.getByRole('tab', { name: /Facilities/ }));
    fireEvent.change(await screen.findByLabelText('Operating zone'), { target: { value: 'cbd' } });
    const facility = await screen.findByTestId('facility-mock-cbd-01');
    expect(facility).toHaveTextContent('CBD simulated parking 1');
    expect(facility).toHaveTextContent('12 / 90');
    expect(facility).toHaveTextContent('6 / 12');
    expect(screen.getByText(/Batch recommendations evaluate and internally reserve simulated spaces/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Dispatch' }));
    fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recommend EV' })).toBeEnabled());
  });
  it('keeps observations available but blocks new destinations when the provider is unknown', async () => {
    server({ '/api/facilities/facilities': new Error('Facilities down') });
    render(<App/>);
    expect(await screen.findByText(/Facilities \(mock\) · unavailable/)).toBeInTheDocument();
    expect(await screen.findByTestId('vehicle-ev-01')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /CBD Critical/ }));
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
    fireEvent.click(screen.getByRole('tab', { name: 'Fleet' }));
    const row = await screen.findByTestId('vehicle-ev-01');
    expect(row).toHaveTextContent('UNAVAILABLE');
    const detail = screen.getByRole('region', { name: 'Selected vehicle details' });
    expect(detail).toHaveTextContent('Rented');
    expect(detail).toHaveTextContent('Expected return');
    fireEvent.click(screen.getByRole('tab', { name: 'Dispatch' }));
    fireEvent.click(screen.getByRole('button', { name: /CBD Critical/ }));
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
    fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
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
    expect(result).toHaveTextContent('Approved');
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
    fireEvent.click(screen.getByRole('tab', { name: /Facilities/ }));
    await screen.findByText('LTA DATAMALL · SIMULATED ALLOCATIONS');
    fireEvent.change(await screen.findByLabelText('Operating zone'), { target: { value: 'cbd' } });
    const card = await screen.findByTestId('facility-lta-parking-LTA-A1');
    expect(card).toHaveTextContent('3 / total unknown');
    expect(card).toHaveTextContent('Reported by LTA before simulated allocations4');
    expect(card).toHaveTextContent('Charging bays availableUnknown');
    expect(screen.getByText(/LTA charging: ERROR/)).toHaveTextContent('LTA_AUTH_FAILED');
    fireEvent.click(screen.getByRole('tab', { name: 'Dispatch' }));
    fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
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
    fireEvent.click(screen.getByRole('tab', { name: /Facilities/ }));
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
    fireEvent.click(screen.getByRole('tab', { name: /Charging/ }));
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
    fireEvent.click(screen.getByRole('tab', { name: /Charging/ }));
    expect(await screen.findByText(/BELOW_OPERATIONAL_RESERVE/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Prepare charger route' })).not.toBeInTheDocument();
  });
  it('shows disabled LTA traffic without implying clear roads', async () => {
    server(); render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Traffic/ }));
    const panel = await screen.findByRole('region', { name: 'Traffic conditions and roadworks' });
    expect(within(panel).getByText(/Live LTA traffic ranking is disabled/)).toBeInTheDocument();
    expect(within(panel).getByText('DISABLED')).toBeInTheDocument();
  });

  it('reports an unavailable traffic service without calling it disabled', async () => {
    server({ '/api/rebalancing/traffic/conditions': new Error('Rebalancing down') });
    render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Traffic/ }));
    const panel = await screen.findByRole('region', { name: 'Traffic conditions and roadworks' });
    expect(await within(panel).findByText(/Traffic feed unavailable: Rebalancing down/)).toBeInTheDocument();
    expect(within(panel).getByText('UNAVAILABLE')).toBeInTheDocument();
    expect(within(panel).queryByText(/Live LTA traffic ranking is disabled/)).not.toBeInTheDocument();
    expect(openNotifications().getByText(/Traffic: Rebalancing down/)).toBeVisible();
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
    fireEvent.click(screen.getByRole('tab', { name: /Traffic/ }));
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
    fireEvent.click(screen.getByRole('tab', { name: 'Fleet' }));
    fireEvent.click(await screen.findByRole('button', { name: 'EV digital twin diagnostics' }));
    const panel = await screen.findByRole('region', { name: 'EV digital twin diagnostics' });
    expect(within(panel).getByText('28 km/h')).toBeInTheDocument();
    expect(within(panel).getByText('32°C')).toBeInTheDocument();
    expect(within(panel).getByText('SIMULATED')).toBeInTheDocument();
    expect(await within(panel).findByText(/2 accepted samples shown/)).toBeInTheDocument();
    expect(within(panel).getByRole('img', { name: 'Historical battery state of charge' })).toBeInTheDocument();
    expect(fetch.mock.calls.some(([path, init]) =>
      String(path).includes('telemetry-history') && init?.method === 'POST')).toBe(false);
  });

  it('shows a simulated closure as fictional rather than live LTA traffic', async () => {
    const now = new Date().toISOString();
    server({ '/api/rebalancing/traffic/conditions': {
      source:'SIMULATION', mode:'SIMULATION', status:'FRESH', generated_at:now,
      scenario_version:1, feeds:{}, speeds:[], incidents:[], roadworks:[],
      simulated_events:[{
        id:'sim-1', dispatch_id:'dispatch-00000000', segment_index:2,
        kind:'ROAD_CLOSED', speed_kph:null, source:'SIMULATION',
        start:{latitude:1.30,longitude:103.80},
        end:{latitude:1.305,longitude:103.80}, created_at:now, expires_at:now,
      }],
    }});
    render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Traffic/ }));
    const panel = await screen.findByRole('region', {name:'Traffic conditions and roadworks'});
    expect(within(panel).getByText('SIMULATION MODE')).toBeInTheDocument();
    expect(within(panel).getByText('ROAD CLOSED')).toBeInTheDocument();
    expect(within(panel).getByText(/separately labelled from live LTA observations/)).toBeInTheDocument();
    expect(within(panel).getByText(/Road closed/)).toBeInTheDocument();
  });

  it('shows live LTA observations alongside a separately labelled simulated closure', async () => {
    const now = new Date().toISOString();
    const feeds = { speeds:{status:'FRESH', observed_at:now, count:1, error:null},
      incidents:{status:'FRESH', observed_at:now, count:1, error:null},
      roadworks:{status:'FRESH', observed_at:now, count:1, error:null} };
    server({ '/api/rebalancing/traffic/conditions': {
      source:'SIMULATION', mode:'LIVE_AND_SIMULATION', status:'FRESH', generated_at:now,
      scenario_version:1, feeds:{}, speeds:[], incidents:[], roadworks:[],
      simulated_events:[{ id:'sim-combined', dispatch_id:'dispatch-combined', segment_index:1,
        kind:'ROAD_CLOSED', speed_kph:null, source:'SIMULATION',
        start:{latitude:1.30,longitude:103.80}, end:{latitude:1.305,longitude:103.80},
        created_at:now, expires_at:now }],
      live_observations:{ source:'LTA_DATAMALL', status:'FRESH', generated_at:now, feeds,
        speeds:[{id:'live-speed',road:'OBSERVED ROAD',start:{latitude:1.31,longitude:103.81},
          end:{latitude:1.32,longitude:103.82},min_kph:10,max_kph:20}],
        incidents:[{id:'live-incident',type:'ROAD BLOCK',position:{latitude:1.31,longitude:103.81},
          message:'Observed LTA closure'}],
        roadworks:[{id:'live-work',road:'OBSERVED ROAD',start_date:'2020-01-01',end_date:'2099-01-01'}] },
    }});
    render(<App/>);
    fireEvent.click(screen.getByRole('tab', { name: /Traffic/ }));
    const panel = await screen.findByRole('region', {name:'Traffic conditions and roadworks'});
    expect(within(panel).getByText('LIVE LTA + SIMULATION')).toBeInTheDocument();
    expect(within(panel).getByText('Observed LTA closure')).toBeInTheDocument();
    expect(within(panel).getByText('ROAD CLOSED')).toBeInTheDocument();
  });

});

describe('CRDS recommendation explanations', () => {
  const crds: NonNullable<ReturnType<typeof makeRecommendation>['crds']> = {
    decision: 'DEFER', reason_code: 'ROUTE_EVIDENCE_UNAVAILABLE', approval_allowed: false,
    worst_case_gain_vehicle_minutes: null, worst_case_future_id: null, shortage_changes_by_zone: {},
    arrival_minutes_min: null, arrival_minutes_max: null,
    scenario_count: 4, scenario_model_version: 'fixture', epoch_id: 'assessment-ref', input_fingerprint: '',
    parameters: { horizon_minutes: 30, slot_minutes: 1, minimum_gain_vehicle_minutes: 1 },
    source_references: {},
  };

  it('shows a routing defer and its per-EV disposition without rendering provider codes', async () => {
    const recommendation = { ...makeRecommendation(), selected_vehicle_id: null, ranked_candidates: [],
      explanation: 'ONEMAP_AUTH_FAILED', crds: { ...crds, source_references: { decision_evidence: {
        version: 1, assessment_reasons: ['ROUTING_ACCESS_UNAVAILABLE'], batch_stop_reasons: [],
        vehicles: {
          'ev-01': { outcome: 'INCOMPLETE', reason: 'ROUTING_ACCESS_UNAVAILABLE', related_reasons: [], dispatch_ids: [] },
          'ev-02': { outcome: 'EXCLUDED', reason: 'OFFLINE', related_reasons: [], dispatch_ids: [] },
        },
      } } } };
    server({ '/api/rebalancing/recommendations': recommendation });
    render(<App/>);
    await selectAndRecommend();
    const result = screen.getByTestId('recommendation');
    expect(result).toHaveTextContent('Assessment unavailable');
    expect(result).toHaveTextContent('OneMap routing credentials were rejected or are missing.');
    expect(screen.getByRole('button', { name: 'Request fresh recommendation' })).toBeEnabled();
    fireEvent.click(within(result).getByRole('tab', { name: 'Evidence' }));
    expect(result).toHaveTextContent('EVs with incomplete assessments (1)');
    expect(result).toHaveTextContent('ev-02 · The EV is offline.');
    expect(result).not.toHaveTextContent(/ONEMAP_AUTH_FAILED|ROUTE_EVIDENCE_UNAVAILABLE/);
  });

  it('explains a move in plain language and why other EVs were not chosen', async () => {
    const row = { related_reasons: [], dispatch_ids: [] };
    server({ '/api/rebalancing/recommendations': { ...makeRecommendation(), crds: { ...crds,
      decision: 'MOVE', reason_code: 'USEFUL_SHORTAGE_REDUCTION', approval_allowed: true,
      worst_case_gain_vehicle_minutes: 12, arrival_minutes_min: 6, arrival_minutes_max: 9,
      source_references: { decision_evidence: { version: 1, assessment_reasons: ['USEFUL_SHORTAGE_REDUCTION'], batch_stop_reasons: [],
        vehicles: {
          'ev-01': { ...row, outcome: 'RECOMMENDED', reason: 'USEFUL_SHORTAGE_REDUCTION', gain_vehicle_minutes: 12 },
          'ev-03': { ...row, outcome: 'NOT_SELECTED', reason: 'LOWER_MODELED_BENEFIT', gain_vehicle_minutes: 4 },
          'ev-04': { ...row, outcome: 'EXCLUDED', reason: 'RENTED' },
          'ev-05': { ...row, outcome: 'EXCLUDED', reason: 'RENTED' },
          'ev-06': { ...row, outcome: 'EXCLUDED', reason: 'OFFLINE' },
        } } } } } });
    render(<App/>);
    await selectAndRecommend();
    const result = screen.getByTestId('recommendation');
    expect(result).toHaveTextContent('Move ev-01 to CBD. Customers there would have one extra EV available for about 12 minutes of the next 30 minutes, even in the busiest of the 4 demand patterns CRDS checked.');
    expect(result).toHaveTextContent('3 EVs could not be considered: 2 rented, 1 offline.');
    expect(result).toHaveTextContent('How does CRDS decide?');
    fireEvent.click(within(result).getByRole('tab', { name: 'Evidence' }));
    expect(result).toHaveTextContent('ev-01 was chosen instead: it adds about 12 minutes of EV availability, compared with about 4 minutes for this EV.');
    expect(result).not.toHaveTextContent(/points|coverage score/);
  });

  it('explains how close the best option came when CRDS recommends waiting', async () => {
    server({ '/api/rebalancing/recommendations': { ...makeRecommendation(), selected_vehicle_id: null, crds: { ...crds,
      decision: 'HOLD', reason_code: 'NO_USEFUL_MOVE',
      source_references: { decision_evidence: { version: 1, assessment_reasons: ['NO_USEFUL_MOVE'], batch_stop_reasons: [],
        vehicles: {
          'ev-07': { outcome: 'NOT_SELECTED', reason: 'NO_USEFUL_MOVE', gain_vehicle_minutes: -39,
            origin_shortage_increase: 30, related_reasons: [], dispatch_ids: [] },
          'ev-08': { outcome: 'NOT_SELECTED', reason: 'NO_USEFUL_MOVE', gain_vehicle_minutes: 0,
            related_reasons: [], dispatch_ids: [] },
          'ev-03': { outcome: 'NOT_SELECTED', reason: 'NO_USEFUL_MOVE', gain_vehicle_minutes: 0.5,
            related_reasons: [], dispatch_ids: [] },
        } } } } } });
    render(<App/>);
    await selectAndRecommend();
    const result = screen.getByTestId('recommendation');
    expect(result).toHaveTextContent(
      'The best option, ev-03, would give customers in CBD one extra EV available for less than a minute of the next 30 minutes. CRDS only recommends a trip that adds more than about 1 minute of availability, so it is better to wait.');
    fireEvent.click(within(result).getByRole('tab', { name: 'Evidence' }));
    expect(result).toHaveTextContent('ev-03 · Moving it would only add less than a minute of EV availability overall, which is not enough to be worth the trip.');
    expect(result).toHaveTextContent('ev-07 · Moving it would do more harm than good: customers in its current zone would be left without a car for about 30 minutes, which outweighs any benefit at the destination.');
    expect(result).toHaveTextContent('ev-08 · Moving it would not help: the destination is expected to have enough cars');
    expect(result).not.toHaveTextContent(/points|coverage score|no time/);
  });

  it('requests a fresh recommendation from a deferred result', async () => {
    const deferred = { ...makeRecommendation(), selected_vehicle_id: null,
      ranked_candidates: [], crds: { ...crds, reason_code: 'DEMAND_UNKNOWN_OR_STALE' } };
    const fetch = server({ '/api/rebalancing/recommendations': deferred });
    render(<App/>);
    await selectAndRecommend();

    const retry = screen.getByRole('button', { name: 'Request fresh recommendation' });
    expect(retry).toBeEnabled();
    Object.assign(deferred, makeRecommendation(), {
      crds: { ...crds, decision: 'MOVE', reason_code: 'USEFUL_SHORTAGE_REDUCTION', approval_allowed: true },
    });
    fireEvent.click(retry);

    await waitFor(() => expect(fetch.mock.calls.filter(([path, init]) =>
      path === '/api/rebalancing/recommendations' && init?.method === 'POST')).toHaveLength(2));
    expect(await screen.findByRole('button', { name: 'Review dispatch' })).toBeEnabled();
  });

  it('renders a readable fallback for an unknown legacy code', async () => {
    server({ '/api/rebalancing/recommendations': { ...makeRecommendation(), selected_vehicle_id: null,
      explanation: 'UNKNOWN_INTERNAL_CODE', crds: { ...crds, reason_code: 'UNKNOWN_INTERNAL_CODE' } } });
    render(<App/>);
    await selectAndRecommend();
    expect(screen.getByTestId('recommendation')).toHaveTextContent('could not verify the information');
    expect(screen.getByTestId('recommendation')).not.toHaveTextContent('UNKNOWN_INTERNAL_CODE');
  });

  it('shows why a partial batch stopped and keeps the proposed EV reviewable', async () => {
    server({ '/api/rebalancing/batch-recommendations': {
      batch_recommendation_id: 'batch-1', target_zone_id: 'cbd', requested_count: 3, recommended_count: 1,
      unfulfilled_count: 2, expires_at: new Date(Date.now() + 30_000).toISOString(),
      explanation: 'CAPACITY_OR_STOCK_LIMIT', assumptions: [],
      assignments: [{ recommendation_id: 'rec-1', vehicle_id: 'ev-01', facility_id: 'cbd-01', facility_name: 'CBD parking' }],
      crds: { ...crds, decision: 'MOVE', approval_allowed: true, worst_case_gain_vehicle_minutes: 12,
        source_references: { decision_evidence: {
          version: 1, assessment_reasons: ['USEFUL_SHORTAGE_REDUCTION', 'PARTIAL_ROUTE_COVERAGE'], batch_stop_reasons: ['BATCH_PARKING_LIMIT'],
          vehicles: {},
        } } },
    } });
    render(<App/>);
    fireEvent.click(await screen.findByRole('button', { name: /CBD Critical/ }));
    fireEvent.change(screen.getByLabelText('Number of EVs needed'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Recommend 3 EVs' }));
    const result = await screen.findByTestId('batch-recommendation');
    expect(result).toHaveTextContent('1 of 3 EVs recommended');
    expect(result).toHaveTextContent('used the remaining parking spaces');
    expect(result).toHaveTextContent('Some routes could not be verified');
    expect(result).not.toHaveTextContent('CAPACITY_OR_STOCK_LIMIT');
    expect(screen.getByRole('button', { name: 'Review dispatch' })).toBeEnabled();
  });
});

describe('approval decision explanations', () => {
  it('shows a structured rejection in Notifications and relocation history', async () => {
    const evidence = { status: 'REJECTED' as const, reason_code: 'PARKING_FULL', vehicle_id: 'ev-01' };
    const rejected = { ...makeDispatch(), state: 'REJECTED', vehicle_id: null, command: null,
      claim_held: false, acknowledged_at: null, failure_reason: 'PARKING_DESTINATION_CHANGED',
      revalidated_metrics: { approval_decision: evidence } };
    const reference = '11ee1111-1111-4111-8111-111111111111';
    const fetch = server();
    const base = fetch.getMockImplementation()!;
    let submitted = false;
    fetch.mockImplementation(async (input, init) => {
      if (String(input) !== '/api/dispatch/dispatches') return base(input, init);
      if (init?.method === 'POST') {
        submitted = true;
        return new Response(JSON.stringify({ error: { code: rejected.failure_reason,
          request_id: reference, approval_decision: evidence } }), { status: 409 });
      }
      return new Response(JSON.stringify({ dispatches: submitted ? [rejected] : [] }));
    });
    render(<App/>);
    await selectAndRecommend();
    fireEvent.click(screen.getByRole('button', { name: 'Review dispatch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 1 EV' }));
    const notifications = openNotifications();
    expect(await notifications.findByText(/parking space is no longer available/)).toHaveTextContent(reference);
    expect(notifications.queryByText(/PARKING_DESTINATION_CHANGED/)).not.toBeInTheDocument();
    fireEvent.click(notifications.getByRole('button', { name: 'Close' }));
    fireEvent.click(screen.getByRole('tab', { name: /Relocations/ }));
    const history = await screen.findByTestId('dispatch-dispatch-1');
    expect(history).toHaveTextContent('ev-01');
    expect(history).toHaveTextContent('Approval rejected.');
    expect(history).toHaveTextContent('The parking space is no longer available.');
  });

  it('uses a safe fallback for an unreadable HTTP error and explains the same request after lookup', async () => {
    const fetch = server();
    const base = fetch.getMockImplementation()!;
    let requestId = '';
    fetch.mockImplementation(async (input, init) => {
      if (String(input) === '/api/dispatch/dispatches' && init?.method === 'POST') {
        requestId = JSON.parse(String(init.body)).request_id;
        return new Response(JSON.stringify({ error: { code: { unexpected: true }, approval_decision: 'bad' } }),
          { status: 503 });
      }
      if (String(input) === `/api/dispatch/approvals/${requestId}`) {
        return new Response(JSON.stringify({ ...makeDispatch(), request_id: requestId, state: 'FAILED',
          approval_status: 'APPROVED', failure_reason: 'BATTERY_DEPLETED',
          revalidated_metrics: { approval_decision: { status: 'APPROVED', reason_code: 'CRDS_APPROVAL_CONFIRMED' } },
        }));
      }
      return base(input, init);
    });
    render(<App/>);
    await selectAndRecommend();
    fireEvent.click(screen.getByRole('button', { name: 'Review dispatch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 1 EV' }));
    const notifications = openNotifications();
    expect(await notifications.findByText(/Approval is still unconfirmed/)).toBeVisible();
    fireEvent.click(notifications.getByRole('button', { name: 'Check status' }));
    expect(await notifications.findByText(/ev-01 · Approved/)).toHaveTextContent('ran out of battery during relocation');
    expect(notifications.queryByText(/BATTERY_DEPLETED|unexpected/)).not.toBeInTheDocument();
  });
});
