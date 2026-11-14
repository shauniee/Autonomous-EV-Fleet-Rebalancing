// Wire fields consumed from the existing Pydantic contracts; no business rules here.
export interface Position { latitude: number; longitude: number }
export interface TrafficRouteAssessment {
  status: string; blocked: boolean; delay_factor: number | null;
  estimated_duration_seconds: number | null; risk: number | null;
  matched_speed_segments: number; roadworks: number; incidents: number;
}
export interface TrafficSegment { id: string; road: string; start: Position; end: Position; min_kph: number; max_kph: number }
export interface TrafficIncident { id: string; type: string; position: Position; message: string }
export interface SimulatedTrafficEvent {
  id: string; dispatch_id: string; segment_index: number;
  kind: 'CONGESTION' | 'ROADWORKS' | 'ROAD_CLOSED'; speed_kph: number | null;
  source: 'SIMULATION'; start: Position; end: Position;
  created_at: string; expires_at: string;
}
export interface ObservedTrafficSnapshot {
  source: 'LTA_DATAMALL'; status: string; generated_at: string;
  speed_display_count?: number;
  feeds: Record<string, {status: string; observed_at: string | null; count: number; error: string | null}>;
  speeds: TrafficSegment[]; incidents: TrafficIncident[];
  roadworks: {id: string; road: string; start_date: string; end_date: string}[];
}
export interface TrafficSnapshot {
  source: string; mode?: 'LIVE' | 'SIMULATION' | 'LIVE_AND_SIMULATION' | 'DISABLED';
  status: string; generated_at: string;
  speed_display_count?: number;
  simulated_events?: SimulatedTrafficEvent[]; scenario_version?: number;
  live_observations?: ObservedTrafficSnapshot | null;
  feeds: Record<string, {status: string; observed_at: string | null; count: number; error: string | null}>;
  speeds: TrafficSegment[]; incidents: TrafficIncident[];
  roadworks: {id: string; road: string; start_date: string; end_date: string}[];
}
export interface RoadRoute {
  source: 'ONEMAP' | 'OSRM'; fetched_at: string; geometry: Position[];
  rerouted?: boolean; traffic_assessment?: TrafficRouteAssessment | null;
  distance_km: number; duration_seconds: number;
  instructions: { text: string; distance_m: number }[];
}
export interface Zone {
  zone_id: string; name: string; position: Position;
  rental_demand: number | null; demand_level: string; demand_status: string;
  demand_observed_at?: string | null;
  available_evs: number | null; inbound_relocating_evs: number | null;
  inbound_pending_evs: number | null; inbound_unconfirmed_evs: number | null;
  current_shortage: number | null; projected_shortage: number | null;
  current_surplus: number | null; projected_surplus: number | null;
  hotspot_severity: 'NORMAL' | 'WARNING' | 'CRITICAL' | 'UNKNOWN'; data_quality: string;
}
export interface VehiclePlacement {
  zone_id: string | null;
  parking: { facility_id: string; name: string } | null;
}
export interface HeatMap {
  generated_at: string; demand_snapshot_at: string; fleet_snapshot_at: string | null;
  status: string; issues: string[]; fleet_status: string; zones: Zone[];
  available_outside_zones?: number | null;
  vehicle_placements?: Record<string, VehiclePlacement> | null;
  policy?: { medium_demand: number; high_demand: number };
}
export interface ChargerAdvice {
  vehicle_id: string; current_soc_pct: number;
  status: 'RECOMMENDED' | 'NO_SAFE_CHARGER' | 'INELIGIBLE';
  reasons: string[];
  charger: {
    facility_id: string; facility_name: string; zone_id: string;
    source: 'MOCK' | 'LTA_DATAMALL'; distance_km: number;
    energy_required_kwh: number; predicted_arrival_soc_pct: number;
    charging_available: number; charging_power_kw: number | null;
  } | null;
}
export interface ChargingAdvice {
  generated_at: string; trigger_soc_pct: number; min_arrival_reserve_pct: number;
  vehicles: ChargerAdvice[]; assumptions: string[];
}
export interface VehicleParking {
  vehicle_id: string;
  pending: boolean;
  parking: {
    facility_id: string; name: string; charging_allocated: boolean;
    charging_session_id: string | null; charging_target_soc_pct: number;
    cable_status: 'AWAITING_CONNECTION' | 'CONNECTED' | 'DISCONNECTED';
    cable_connected_by?: string | null; cable_connected_at?: string | null;
    cable_disconnected_by?: string | null; cable_disconnected_at?: string | null;
  } | null;
}
export interface ChargingTask {
  task_id: string; vehicle_id: string;
  status: 'PENDING' | 'ASSIGNED' | 'IN_PROGRESS' | 'COMPLETED' | 'CANCELLED';
  soc_at_creation: number; assigned_staff: string | null;
  created_at: string; updated_at: string;
}
export interface Rental { rental_id: string; status: 'RENTED' | 'RETURNED'; expected_end_at: string; expected_return_zone_id?: string | null; started_at: string; return_position: Position | null; return_soc_pct: number | null; }
export interface Vehicle {
  simulator_runtime?: {
    status: 'RESPONSIVE' | 'UNRESPONSIVE' | 'STALE';
    mode: 'STARTING' | 'RUNNING' | 'RETRYING' | 'RECOVERY_WAIT' | 'STOPPED';
    live: boolean; ready: boolean; blocking_reason: string | null;
    heartbeat_age_seconds: number | null; report_age_seconds: number;
    checkpoint_revision: number; checkpoint_at: string | null; reported_at: string;
  } | null;
  active_rental?: Rental | null;
  active_charging_task?: ChargingTask | null;
  active_claim?: unknown | null;
  state_version: number;
  vehicle_id: string; current_zone_id: string | null; stale: boolean; connectivity: string;
  effective_availability: string; effective_relocation_state: string; received_at: string | null;
  telemetry: { position: Position; soc_pct: number; observed_at: string;
    session_id: string; sequence: number;
    active_dispatch_id?: string | null; distance_travelled_km?: number | null;
    parked_facility_id?: string | null; charging_state?: 'NOT_CHARGING' | 'AWAITING_CONNECTION' | 'CHARGING' | 'COMPLETE';
    charging_power_kw?: number | null;
    speed_kph?: number | null; heading_deg?: number | null;
    odometer_km?: number | null; battery_temp_c?: number | null;
    motor_temp_c?: number | null; power_kw?: number | null;
    battery_soh_pct?: number | null; gps_quality?: string | null;
    fault_codes?: string[] } | null;
}
export interface Fleet { instance_id: string; snapshot_at: string; vehicles: Vehicle[] }
export interface Candidate {
  vehicle_id: string; current_soc_pct: number | null; predicted_arrival_soc_pct: number | null;
  distance_km: number | null; energy_required_kwh: number | null; score: number | null;
  charging_assessment: string | null; exclusion_reasons: string[];
  crds_gain?: { worst_case_gain_vehicle_minutes: number; worst_case_future_id: string } | null;
}
export interface CrdsRelocationExclusion {
  vehicle_id: string;
  reason: 'TELEMETRY_UNAVAILABLE' | 'RELOCATION_FAILED' | 'RELOCATION_UNCONFIRMED'
    | 'RELOCATION_OWNERSHIP_UNCONFIRMED' | 'VEHICLE_MISSING';
  dispatch_ids: string[];
}
export interface CrdsVehicleDecision {
  outcome: 'RECOMMENDED' | 'EXCLUDED' | 'NOT_SELECTED' | 'INCOMPLETE';
  reason: string;
  facility_id?: string;
  related_reasons: string[];
  dispatch_ids: string[];
  arrival_soc_pct?: number;
  reserve_pct?: number;
  gain_vehicle_minutes?: number;
  origin_shortage_increase?: number;
}
export interface CrdsDecisionEvidence {
  version: 1;
  assessment_reasons: string[];
  batch_stop_reasons: string[];
  vehicles: Record<string, CrdsVehicleDecision>;
}
export interface CrdsMetadata {
  planning_mode?: 'OPTIMIZE_COVERAGE' | 'FULFIL_REQUEST';
  coverage_tradeoff?: boolean;
  decision: 'MOVE' | 'HOLD' | 'DEFER'; reason_code: string; approval_allowed: boolean;
  worst_case_gain_vehicle_minutes: number | null; worst_case_future_id: string | null;
  shortage_changes_by_zone: Record<string, number>;
  arrival_minutes_min: number | null; arrival_minutes_max: number | null;
  scenario_count: number; scenario_model_version: string; epoch_id: string; input_fingerprint: string;
  source_references: Record<string, unknown> & {
    relocation_exclusions?: CrdsRelocationExclusion[];
    excluded_facility_ids?: string[];
    decision_evidence?: CrdsDecisionEvidence;
  };
  parameters: { horizon_minutes: number; slot_minutes: number; minimum_gain_vehicle_minutes: number;
    priority_coverage_pct?: number };
}
export interface Recommendation {
  recommendation_id: string; expires_at: string; created_at: string;
  selected_vehicle_id: string | null; target: { zone_id: string; name: string };
  ranked_candidates: Candidate[]; excluded_candidates: Candidate[]; explanation: string; assumptions: string[];
  crds?: CrdsMetadata | null;
}
export interface RecoveryOperation {
  request_id: string; status: 'REQUESTED' | 'RESTORING' | 'RECOVERED' | 'BLOCKED';
  reason_code: string | null;
}
export interface RecoveryRequest {
  request_id: string; action: 'RESUME_SIMULATION'; expected_assessment_revision: string;
}
export interface RecoveryAssessment {
  dispatch_id: string; vehicle_id: string | null; assessed_at: string; assessment_revision: string;
  status: 'NOT_REQUIRED' | 'REQUIRED' | 'BLOCKED'; reason_code: string; can_resume: boolean;
  supported_action: 'RESUME_SIMULATION' | null; operation: RecoveryOperation | null;
  blockers: { code: string; detail: string }[];
  evidence: { checkpoint_source: 'LEGACY_TELEMETRY' | 'SIMULATOR_CHECKPOINT' | 'NONE'; checkpoint_at: string | null;
    recorded_position?: Position | null; recorded_soc_pct?: number | null;
    claim_status?: string | null; parking_status?: string | null };
}
export interface RelocationEndRequest {
  request_id: string; expected_assessment_revision: string; reason: string;
}
export interface RelocationResolution {
  assessment_revision: string | null; can_end: boolean; unstarted?: boolean; blocker: string | null;
  stop_position: Position | null; stop_soc_pct: number | null;
  operation: {
    request_id: string; reason: string;
    status: 'REQUESTED' | 'CLEANUP_PENDING' | 'ENDED' | 'BLOCKED';
    detail: string | null;
  } | null;
}
export interface Dispatch {
  dispatch_id: string; request_id: string; recommendation_id?: string; vehicle_id: string | null; state: string;
  command: { target_zone_id: string; target_position: Position; route?: RoadRoute | null } | null;
  created_at: string; acknowledged_at: string | null; last_contact_at: string | null;
  distance_travelled_km: number; arrival_soc_pct: number | null; failure_reason: string | null;
  parking_facility_id?: string | null; parking_status?: string | null;
  recovery?: RecoveryOperation | null;
  claim_held: boolean | null;
  revalidated_metrics: {
    distance_km?: number;
    approval_decision?: ApprovalDecision;
    origin?: { position: Position | null; zone_id: string | null; facility_id: string | null };
  } | null;
}
export interface DispatchList { dispatches: Dispatch[] }
export interface Approval { request_id: string; recommendation_id: string }

// Simulated facilities become capacity-aware for approved bulk dispatch.
export interface Facility {
  facility_id: string; name: string; zone_id: string; position: Position;
  parking_capacity: number | null; parking_available: number;
  charging_bays: number | null; charging_available: number;
  connector_types: string[]; status: 'OPEN' | 'CLOSED' | 'UNKNOWN'; source: 'MOCK' | 'LTA_DATAMALL';
  facility_type?: 'PARKING' | 'CHARGING'; availability_basis?: 'PARKING_BAYS' | 'CHARGERS';
  agency?: string | null; address?: string | null;
  availability_observed_at?: string | null; availability_expires_at?: string | null;
  reported_parking_available?: number | null; reported_charging_available?: number | null;
  charging_power_kw?: number | null;
}
export interface FacilityFeed {
  name: 'parking' | 'charging'; status: 'PENDING' | 'MISSING_KEY' | 'FRESH' | 'STALE' | 'ERROR';
  last_success_at: string | null; error_code: string | null;
}
export interface FacilitiesSnapshot {
  generated_at: string; data_source: 'MOCK' | 'LTA_DATAMALL'; facilities: Facility[]; feeds?: FacilityFeed[];
}

export interface ParkingAssignment {
  recommendation_id: string; vehicle_id: string; facility_id: string; facility_name: string;
  target_position: Position; requires_charging: boolean; distance_km: number;
  predicted_arrival_soc_pct: number; score: number | null; traffic_assessment?: TrafficRouteAssessment | null;
  crds_gain?: { worst_case_gain_vehicle_minutes: number; worst_case_future_id: string } | null;
}
export interface BatchRecommendation {
  crds?: CrdsMetadata | null;
  unfulfilled_reason?: string | null;
  batch_recommendation_id: string; target_zone_id: string;
  purpose?: 'REBALANCE' | 'CHARGE';
  requested_count: number; recommended_count: number; unfulfilled_count: number;
  traffic_status?: string; traffic_generated_at?: string;
  created_at: string; expires_at: string; explanation: string;
  assignments: ParkingAssignment[]; assumptions: string[];
}
export interface BatchApproval { request_id: string; batch_recommendation_id: string }
export type ApprovalStatus = "APPROVED" | "REJECTED" | "UNKNOWN";
export interface ApprovalDecision {
  status: ApprovalStatus;
  reason_code: string;
  vehicle_id?: string;
  facility_id?: string;
  related_reasons?: string[];
  arrival_soc_pct?: number;
  reserve_pct?: number;
  gain_vehicle_minutes?: number;
  minimum_gain_vehicle_minutes?: number;
}
export type DispatchApprovalResult = Dispatch & { approval_status?: ApprovalStatus };
export interface BatchDispatchItem {
  approval_status?: ApprovalStatus;
  approval_decision?: ApprovalDecision | null;
  recommendation_id: string; vehicle_id: string; facility_id: string;
  dispatch_id: string | null; state: string; error: string | null;
  queue_status?: 'QUEUED' | 'WAITING' | 'ADMITTED' | 'REJECTED' | 'CANCELLED' | null;
  attempts?: number; retry_at?: string | null;
}
export interface BatchDispatch {
  request_id: string; batch_recommendation_id: string; requested_count: number;
  recommended_count: number; items: BatchDispatchItem[];
  status: 'APPROVED' | 'PARTIAL' | 'REJECTED' | 'UNKNOWN' | 'QUEUED' | 'PROCESSING';
  job_status?: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'EXPIRED' | null;
  accepted_at?: string | null; expires_at?: string | null;
  admitted_count?: number; completed_count?: number; queued_count?: number; in_flight_limit?: number;
}


export interface RecommendationDraft {
  planning_mode?: 'OPTIMIZE_COVERAGE' | 'FULFIL_REQUEST';
  request_id: string;
  target_zone_id: string;
  quantity: number;
  vehicle_id?: string | null;
  vehicle_ids?: string[] | null;
}

export interface RecommendationRequest extends RecommendationDraft {
  status: 'QUEUED' | 'EVALUATING' | 'READY' | 'HOLD' | 'DEFERRED' | 'ADVISORY' | 'FAILED' | 'EXPIRED';
  submitted_at: string;
  batch_recommendation_id: string | null;
  approval_request_id: string | null;
  recommended_count: number | null;
  expires_at: string | null;
  reason_code: string | null;
  assignments: { vehicle_id: string; facility_id: string; facility_name: string }[];
  vehicle_reasons?: Record<string, string>;
  batch: BatchRecommendation | null;
}

export interface RecommendationRequestPage {
  enabled: boolean;
  crds_mode: 'DISABLED' | 'ADVISORY' | 'ACTIVE';
  requests: RecommendationRequest[];
  next_offset: number | null;
}

export interface BatchApprovalLookup {
  batches: BatchDispatch[];
  refusals: { request_id: string; batch_recommendation_id: string; reason_code: string }[];
}

export interface BatchApprovalGroupRequest {
  group_id: string;
  batch_recommendation_ids: string[];
}

export interface BatchApprovalGroupPlan {
  batch_recommendation_id: string;
  request_id: string;
  status: BatchDispatch['status'] | 'QUEUED' | 'RUNNING' | 'SKIPPED';
  reason_code: string | null;
  approval: BatchDispatch | null;
}

export interface BatchApprovalGroup {
  group_id: string;
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'CONFLICT' | 'UNKNOWN';
  created_at: string;
  updated_at: string;
  reason_code: string | null;
  plans: BatchApprovalGroupPlan[];
  conflicts: { vehicle_id: string; batch_recommendation_ids: string[] }[];
}

export interface BatchApprovalGroupPage {
  enabled: boolean;
  groups: BatchApprovalGroup[];
  next_offset: number | null;
}
