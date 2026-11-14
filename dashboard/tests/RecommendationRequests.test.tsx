import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BatchDispatch, CrdsMetadata, RecommendationRequest } from '../src/api/contracts';
import { CrdsSummary } from '../src/components/CrdsSummary';
import { requestApprovalStatus } from '../src/components/RecommendationRequests';

const request: RecommendationRequest = {
  request_id: 'request', target_zone_id: 'cbd', quantity: 50, planning_mode: 'FULFIL_REQUEST',
  status: 'READY', submitted_at: new Date().toISOString(), batch_recommendation_id: 'batch',
  approval_request_id: 'approval', recommended_count: 1, expires_at: new Date(Date.now() + 60000).toISOString(),
  reason_code: null, assignments: [], batch: null,
};
const progress: BatchDispatch = {
  request_id: 'approval', batch_recommendation_id: 'batch', requested_count: 50,
  recommended_count: 1, status: 'PROCESSING', job_status: 'RUNNING', items: [],
};

describe('queued recommendation dispatch progress', () => {
  it.each(['RUNNING', 'EXPIRED'] as const)('keeps a %s paced job in progress without offering fresh approval', job_status => {
    const state = requestApprovalStatus(request, Date.now(), {
      batches: [{ ...progress, job_status }], refusals: [],
    });
    expect(state.running).toBe(true);
    expect(state.ready).toBe(false);
    expect(state.status).toBe(job_status === 'EXPIRED'
      ? 'Authorization expired; existing work finishing' : 'Relocations in progress');
  });

  it('moves a terminal paced job to history after existing work finishes', () => {
    const state = requestApprovalStatus(request, Date.now(), {
      batches: [{ ...progress, status: 'PARTIAL', job_status: 'EXPIRED' }], refusals: [],
    });
    expect(state.running).toBe(false);
    expect(state.ready).toBe(false);
    expect(state.status).toBe('Approval window expired');
  });

  it('shows a negative coverage change and advisory explanation when fulfilling a quantity', () => {
    const result: CrdsMetadata = {
      decision: 'MOVE', reason_code: 'OPERATOR_REQUESTED_QUANTITY', approval_allowed: true,
      planning_mode: 'FULFIL_REQUEST', coverage_tradeoff: true, worst_case_gain_vehicle_minutes: -12,
      worst_case_future_id: 'future', shortage_changes_by_zone: {}, arrival_minutes_min: 2,
      arrival_minutes_max: 3, scenario_count: 4, scenario_model_version: 'test', epoch_id: 'epoch',
      input_fingerprint: 'input', source_references: {},
      parameters: { horizon_minutes: 30, slot_minutes: 1, minimum_gain_vehicle_minutes: 1 },
    };
    render(<CrdsSummary result={result} batchStop={null}/>);
    const summary = screen.getByRole('region', { name: 'Fleet-wide availability assessment' });
    expect(summary).toHaveTextContent('-12 vehicle-minutes');
    expect(summary).toHaveTextContent('may increase shortages in other zones');
    expect(summary).toHaveTextContent('Feasible relocations can be planned even when they reduce fleet coverage');
  });
});
