import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { BatchConfirmation, BatchResult } from '../src/components/BatchPanel';
import { RelocationPanel } from '../src/components/RelocationPanel';
import { makeDispatch, makeFleet } from './fixtures';
import type { BatchDispatch, BatchRecommendation } from '../src/api/contracts';

const recommendation = {
  batch_recommendation_id: 'batch-paced', target_zone_id: 'cbd', purpose: 'CHARGE',
  requested_count: 50, recommended_count: 2, unfulfilled_count: 48,
  created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 30000).toISOString(),
  explanation: 'Two eligible EVs', assumptions: [], assignments: [{
    recommendation_id: 'rec-1', vehicle_id: 'ev-1', facility_id: 'parking-1',
    facility_name: 'CBD charging', target_position: { latitude: 1.28, longitude: 103.85 },
    requires_charging: true, distance_km: 1, predicted_arrival_soc_pct: 25, score: null,
  }],
} as BatchRecommendation;

const queued = {
  request_id: 'request-paced', batch_recommendation_id: 'batch-paced', requested_count: 50,
  recommended_count: 2, status: 'QUEUED', job_status: 'QUEUED',
  accepted_at: '2026-11-14T08:00:00Z', expires_at: '2026-11-14T08:30:00Z',
  admitted_count: 0, completed_count: 0, queued_count: 2, in_flight_limit: 4,
  items: [{ recommendation_id: 'rec-1', vehicle_id: 'ev-1', facility_id: 'parking-1',
    dispatch_id: 'dispatch-1', state: 'QUEUED', error: null,
    approval_status: 'UNKNOWN', queue_status: 'QUEUED', attempts: 0, retry_at: null },
  { recommendation_id: 'rec-2', vehicle_id: 'ev-2', facility_id: 'parking-1',
    dispatch_id: 'dispatch-2', state: 'QUEUED', error: 'ONEMAP_TIMEOUT',
    approval_status: 'UNKNOWN', queue_status: 'WAITING', attempts: 2, retry_at: '2026-11-14T08:00:10Z' }],
} as BatchDispatch;

describe('paced bulk relocation progress', () => {
  it('explains background starts, the approval window and cable confirmation before approval', () => {
    const approve = vi.fn();
    render(<BatchConfirmation batch={recommendation} expired={false} onApprove={approve} onClose={vi.fn()}/>);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('30 minutes');
    expect(dialog).toHaveTextContent('start gradually');
    expect(dialog).toHaveTextContent('automatically');
    expect(dialog).toHaveTextContent('same EVs and destinations');
    expect(dialog).toHaveTextContent('staff must confirm cable connection');
    expect(dialog).toHaveTextContent('48 unfulfilled of 50 requested');
    expect(approve).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve 2 EVs' }));
    expect(approve).toHaveBeenCalledOnce();
  });

  it('separates durable acceptance, automatic waiting, admission and journey completion', () => {
    const close = vi.fn();
    render(<BatchResult result={queued} onClose={close}/>);
    const result = screen.getByRole('region', { name: 'Batch approval outcomes' });
    expect(result).toHaveTextContent('Human approval accepted');
    expect(result).toHaveTextContent('0 admitted');
    expect(result).toHaveTextContent('0 arrived');
    expect(result).toHaveTextContent('2 queued');
    expect(result).toHaveTextContent('48 assignments unfilled');
    expect(result).toHaveTextContent('Waiting · automatic retry');
    expect(result).toHaveTextContent('Attempt 2');
    expect(result).not.toHaveTextContent('Request a fresh recommendation');
    expect(result).not.toHaveTextContent('Approval is still unconfirmed');
    expect(within(result).getAllByRole('button', { name: 'Close unstarted approval' })).toHaveLength(2);
    fireEvent.click(within(result).getAllByRole('button', { name: 'Close unstarted approval' })[0]);
    expect(close).toHaveBeenCalledWith('rec-1');
  });

  it('does not call an admitted EV arrived or offer to close its uncertain claim', () => {
    render(<BatchResult result={{ ...queued, status: 'PROCESSING', job_status: 'RUNNING',
      admitted_count: 1, queued_count: 0, items: [{ ...queued.items[0],
        queue_status: 'ADMITTED', approval_status: 'APPROVED', state: 'SENT' }] }} onClose={vi.fn()}/>);
    expect(screen.getByRole('region', { name: 'Batch approval outcomes' })).toHaveTextContent('Sent to EV');
    expect(screen.getByText(/0 arrived/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Close unstarted approval' })).not.toBeInTheDocument();
  });

  it('shows terminal failures and expiry without promising a completed relocation', () => {
    const view = render(<BatchResult result={{ ...queued, status: 'PARTIAL', job_status: 'COMPLETED',
      queued_count: 0, items: [{ ...queued.items[0], queue_status: 'REJECTED',
        state: 'REJECTED', approval_status: 'REJECTED', error: 'PARKING_FULL' }] }}/>);
    expect(screen.getByRole('region', { name: 'Batch approval outcomes' })).toHaveTextContent('Finished with issues');
    expect(screen.getByText(/parking space is no longer available/)).toBeInTheDocument();
    view.rerender(<BatchResult result={{ ...queued, job_status: 'EXPIRED', status: 'REJECTED',
      queued_count: 0, items: [{ ...queued.items[0], queue_status: 'CANCELLED', state: 'REJECTED',
        approval_status: 'REJECTED', error: 'BATCH_AUTHORIZATION_EXPIRED' }] }}/>);
    expect(screen.getByRole('region', { name: 'Batch approval outcomes' })).toHaveTextContent('Approval window expired');
    expect(screen.queryByRole('button', { name: 'Close unstarted approval' })).not.toBeInTheDocument();
  });
});


it('shows a queued relocation as accepted without manual retry or premature recovery', () => {
  const fleet = makeFleet();
  fleet.vehicles[0].stale = true;
  render(<RelocationPanel dispatches={[{ ...makeDispatch(), state: 'QUEUED',
    command: null, claim_held: null, acknowledged_at: null }]} vehicles={fleet.vehicles}
    zones={[]} facilities={[]} onTrack={vi.fn()} notify={vi.fn()}
    onRetryApproval={vi.fn()} onCloseApproval={vi.fn()}/>);
  const card = screen.getByTestId('dispatch-dispatch-1');
  expect(card).toHaveTextContent('Human approval accepted');
  expect(card).toHaveTextContent('Queued for admission');
  expect(card).not.toHaveTextContent('Approval unconfirmed');
  expect(within(card).queryByRole('button', { name: 'Retry same approval' })).not.toBeInTheDocument();
  expect(within(card).queryByRole('button', { name: /Resume relocation|End relocation/ })).not.toBeInTheDocument();
});


it('keeps expired processing work visible while prohibiting fresh starts', () => {
  const relocation = vi.fn();
  render(<BatchResult result={{ ...queued, job_status: 'EXPIRED', status: 'PROCESSING',
    admitted_count: 1, completed_count: 0, queued_count: 1,
    items: [{ ...queued.items[0], queue_status: 'ADMITTED', approval_status: 'APPROVED', state: 'IN_PROGRESS' },
      { ...queued.items[1], state: 'UNKNOWN', error: 'FLEET_CLAIM_UNCONFIRMED' }] }} onRelocation={relocation}/>);
  const result = screen.getByRole('region', { name: 'Batch approval outcomes' });
  expect(result).toHaveTextContent('Authorization expired');
  expect(result).toHaveTextContent('existing work finishing');
  expect(result).toHaveTextContent('Existing journeys and confirmation of existing claims continue automatically.');
  expect(result).toHaveTextContent('No new relocations will start.');
  expect(result).not.toHaveTextContent('EVs start gradually');
  expect(result).not.toHaveTextContent('A temporary condition is delaying this start.');
  expect(result).not.toHaveTextContent('Request a fresh recommendation');
  const links = within(result).getAllByRole('button', { name: 'View relocation' });
  expect(links).toHaveLength(2);
  fireEvent.click(links[1]);
  expect(relocation).toHaveBeenCalledWith('dispatch-2');
});
