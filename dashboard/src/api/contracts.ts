// Wire fields consumed from the existing Pydantic contracts; no business rules here.
export interface Position { latitude: number; longitude: number }
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
export interface Vehicle {
  vehicle_id: string; current_zone_id: string | null; stale: boolean; connectivity: string;
  effective_availability: string; effective_relocation_state: string; received_at: string | null;
  telemetry: { position: Position; soc_pct: number; observed_at: string } | null;
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
  command: { target_zone_id: string; target_position: Position } | null;
  created_at: string; acknowledged_at: string | null; last_contact_at: string | null;
  distance_travelled_km: number; arrival_soc_pct: number | null; failure_reason: string | null;
  claim_held: boolean | null; revalidated_metrics: { distance_km?: number } | null;
}
export interface DispatchList { dispatches: Dispatch[] }
export interface Claim { origin: Position }
export interface Approval { request_id: string; recommendation_id: string }

// Phase 5A: separate read-only MOCK facilities feed; NOT used by dispatch decisions.
export interface Facility {
  facility_id: string; name: string; zone_id: string; position: Position;
  parking_capacity: number; parking_available: number;
  charging_bays: number; charging_available: number;
  connector_types: string[]; status: 'OPEN' | 'CLOSED'; source: 'MOCK';
}
export interface FacilitiesSnapshot { generated_at: string; data_source: 'MOCK'; facilities: Facility[] }
