import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BatchDispatch, CrdsMetadata, RecommendationRequest } from '../src/api/contracts';
import { CrdsEvidence, CrdsSummary } from '../src/components/CrdsSummary';
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
    expect(summary).toHaveTextContent('loses 12 min of EV cover');
    expect(summary).toHaveTextContent('may increase shortages in other zones');
    expect(summary).toHaveTextContent('Feasible relocations can be planned even when they reduce fleet coverage');
  });

  it('leads a deferred assessment with the EVs that could not be checked', () => {
    const result: CrdsMetadata = {
      decision: 'DEFER', reason_code: 'ROUTE_EVIDENCE_UNAVAILABLE', approval_allowed: false,
      worst_case_gain_vehicle_minutes: null, worst_case_future_id: null, shortage_changes_by_zone: {},
      arrival_minutes_min: null, arrival_minutes_max: null, scenario_count: 6, scenario_model_version: 'test',
      epoch_id: 'epoch', input_fingerprint: 'input',
      parameters: { horizon_minutes: 30, slot_minutes: 1, minimum_gain_vehicle_minutes: 1 },
      source_references: { decision_evidence: { version: 1, assessment_reasons: ['DESTINATION_DATA_CHANGED'],
        batch_stop_reasons: [], vehicles: {
          'ev-scored': { outcome: 'NOT_SELECTED', reason: 'NO_USEFUL_MOVE', related_reasons: [], dispatch_ids: [],
            gain_vehicle_minutes: -5, origin_shortage_increase: 5 },
          'ev-unchecked': { outcome: 'INCOMPLETE', reason: 'DESTINATION_DATA_CHANGED', related_reasons: [], dispatch_ids: [] },
        } } },
    } as CrdsMetadata;
    render(<><CrdsSummary result={result} batchStop={null}/><CrdsEvidence result={result}/></>);
    const summary = screen.getByRole('region', { name: 'Fleet-wide availability assessment' });
    expect(summary).toHaveTextContent('Destination parking data changed or became unavailable');
    expect(summary).not.toHaveTextContent('road routes');
    expect(summary).toHaveTextContent('1 EV could not be checked');
    const evidence = screen.getByRole('region', { name: 'Fleet-wide assessment evidence' });
    expect(screen.getAllByRole('heading', { level: 4 }).map(h => h.textContent))
      .toEqual(['EVs with incomplete assessments (1)', 'EVs not selected (1)']);
    expect(evidence).toHaveTextContent('these are not final decisions');
  });
});
