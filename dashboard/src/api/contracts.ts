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
  feeds: Record<string, {status: string; observed_at: string | null; count: number; error: string | null}>;
  speeds: TrafficSegment[]; incidents: TrafficIncident[];
  roadworks: {id: string; road: string; start_date: string; end_date: string}[];
}
export interface TrafficSnapshot {
  source: string; mode?: 'LIVE' | 'SIMULATION' | 'LIVE_AND_SIMULATION' | 'DISABLED';
  status: string; generated_at: string;
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
  available_evs: number | null; inbound_relocating_evs: number | null;
  inbound_pending_evs: number | null; inbound_unconfirmed_evs: number | null;
  current_shortage: number | null; projected_shortage: number | null;
  current_surplus: number | null; projected_surplus: number | null;
  hotspot_severity: 'NORMAL' | 'WARNING' | 'CRITICAL' | 'UNKNOWN'; data_quality: string;
}
export interface HeatMap {
  generated_at: string; demand_snapshot_at: string; fleet_snapshot_at: string | null;
  status: string; issues: string[]; fleet_status: string; zones: Zone[];
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
export interface ChargingTask {
  task_id: string; vehicle_id: string;
  status: 'PENDING' | 'ASSIGNED' | 'IN_PROGRESS' | 'COMPLETED' | 'CANCELLED';
  soc_at_creation: number; assigned_staff: string | null;
  created_at: string; updated_at: string;
}
export interface Rental { rental_id: string; status: 'RENTED' | 'RETURNED'; expected_end_at: string; expected_return_zone_id?: string | null; started_at: string; return_position: Position | null; return_soc_pct: number | null; }
export interface Vehicle {
  active_rental?: Rental | null;
  active_charging_task?: ChargingTask | null;
  active_claim?: unknown | null;
  state_version: number;
  vehicle_id: string; current_zone_id: string | null; stale: boolean; connectivity: string;
  effective_availability: string; effective_relocation_state: string; received_at: string | null;
  telemetry: { position: Position; soc_pct: number; observed_at: string;
    session_id: string; sequence: number;
    active_dispatch_id?: string | null; distance_travelled_km?: number | null;
    parked_facility_id?: string | null; charging_state?: 'NOT_CHARGING' | 'CHARGING' | 'COMPLETE';
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
}
export interface Recommendation {
  recommendation_id: string; expires_at: string; created_at: string;
  selected_vehicle_id: string | null; target: { zone_id: string; name: string };
  ranked_candidates: Candidate[]; excluded_candidates: Candidate[]; explanation: string; assumptions: string[];
}
export interface Dispatch {
  dispatch_id: string; request_id: string; vehicle_id: string | null; state: string;
  command: { target_zone_id: string; target_position: Position; route?: RoadRoute | null } | null;
  created_at: string; acknowledged_at: string | null; last_contact_at: string | null;
  distance_travelled_km: number; arrival_soc_pct: number | null; failure_reason: string | null;
  parking_facility_id?: string | null; parking_status?: string | null;
  claim_held: boolean | null; revalidated_metrics: { distance_km?: number } | null;
}
export interface DispatchList { dispatches: Dispatch[] }
export interface Claim { origin: Position }
export interface Approval { request_id: string; recommendation_id: string }

// Phase 5C: simulated facilities become capacity-aware for approved bulk dispatch.
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
  predicted_arrival_soc_pct: number; score: number; traffic_assessment?: TrafficRouteAssessment | null;
}
export interface BatchRecommendation {
  batch_recommendation_id: string; target_zone_id: string;
  purpose?: 'REBALANCE' | 'CHARGE';
  requested_count: number; recommended_count: number; unfulfilled_count: number;
  traffic_status?: string; traffic_generated_at?: string;
  created_at: string; expires_at: string; explanation: string;
  assignments: ParkingAssignment[]; assumptions: string[];
}
export interface BatchApproval { request_id: string; batch_recommendation_id: string }
export interface BatchDispatchItem {
  recommendation_id: string; vehicle_id: string; facility_id: string;
  dispatch_id: string | null; state: string; error: string | null;
}
export interface BatchDispatch {
  request_id: string; batch_recommendation_id: string; requested_count: number;
  recommended_count: number; items: BatchDispatchItem[];
  status: 'APPROVED' | 'PARTIAL' | 'REJECTED' | 'UNKNOWN';
}
