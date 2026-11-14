import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { BatchConfirmation, BatchPanel } from '../src/components/BatchPanel';
import type { BatchRecommendation } from '../src/api/contracts';

const batch = {
  batch_recommendation_id: 'operator-batch', target_zone_id: 'cbd', purpose: 'REBALANCE',
  requested_count: 2, recommended_count: 2, unfulfilled_count: 0,
  created_at: '2026-11-12T08:00:00Z', expires_at: '2026-11-12T08:02:00Z',
  assumptions: [], explanation: 'Operator-requested moves', assignments: [{
    recommendation_id: 'operator-child', vehicle_id: 'ev-1', facility_id: 'parking-1',
    facility_name: 'CBD parking', target_position: { latitude: 1.283, longitude: 103.8513 },
    requires_charging: true, distance_km: 8, predicted_arrival_soc_pct: 30, score: null,
  }],
  crds: {
    planning_mode: 'FULFIL_REQUEST', coverage_tradeoff: true,
    decision: 'MOVE', reason_code: 'OPERATOR_REQUESTED_QUANTITY', approval_allowed: true,
    worst_case_gain_vehicle_minutes: -15, worst_case_future_id: 'combined',
    shortage_changes_by_zone: { bishan: 30, cbd: -15 },
    arrival_minutes_min: 2, arrival_minutes_max: 7, scenario_count: 6,
    scenario_model_version: 'crds-futures-v6', epoch_id: 'epoch', input_fingerprint: 'input',
    parameters: { horizon_minutes: 30, slot_minutes: 1, minimum_gain_vehicle_minutes: 1 },
    source_references: {},
  },
} as BatchRecommendation;

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
});
afterEach(cleanup);

describe('operator-requested quantity review', () => {
  it('shows coverage trade-offs without claiming the plan improves coverage', () => {
    render(<BatchPanel batch={batch}/>);
    expect(screen.getByRole('heading', { name: '2 of 2 EVs planned' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Some planned moves do not improve modeled fleet-wide coverage');
    expect(screen.getByText('Worst-case EV cover')).toBeInTheDocument();
    expect(screen.getAllByText(/loses \d+ min of EV cover/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/vehicle-minutes/)).not.toBeInTheDocument();
    expect(screen.queryByText(/The proposed moves improve/)).not.toBeInTheDocument();
  });

  it('keeps a trade-off warning when the whole-plan gain is positive', () => {
    render(<BatchPanel batch={{ ...batch, crds: {
      ...batch.crds!, worst_case_gain_vehicle_minutes: 15,
    } }}/>);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Some planned moves do not improve modeled fleet-wide coverage');
  });

  it('explains the impact and human charging handoff before approval', () => {
    render(<BatchConfirmation batch={batch} expired={false} onApprove={vi.fn()} onClose={vi.fn()}/>);
    const dialog = screen.getByRole('dialog', { hidden: true });
    expect(dialog).toHaveTextContent('Some planned moves do not improve modeled fleet-wide coverage');
    expect(dialog).toHaveTextContent('In the worst scenario the plan loses 15 min of EV cover.');
    expect(dialog).toHaveTextContent('staff must confirm cable connection');
    expect(dialog).not.toHaveTextContent('improves modeled fleet coverage');
    expect(dialog).not.toHaveTextContent('no separate plug-in confirmation');
    expect(within(dialog).getByRole('button', { name: 'Approve 2 EVs', hidden: true })).toBeEnabled();
  });
});


it('describes partial plans accurately in the approval dialog', () => {
  render(<BatchConfirmation batch={{ ...batch, requested_count: 6, unfulfilled_count: 4 }}
    expired={false} onApprove={vi.fn()} onClose={vi.fn()}/>);
  expect(screen.getByRole('dialog', { hidden: true })).toHaveTextContent(
    'This plan covers 2 of 6 requested relocations');
});
