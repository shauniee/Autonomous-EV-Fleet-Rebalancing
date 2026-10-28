import type { Fleet, HeatMap, Recommendation, Dispatch } from '../src/api/contracts';
export const makeHeat = (): HeatMap => ({
  generated_at: new Date().toISOString(), demand_snapshot_at: new Date().toISOString(), fleet_snapshot_at: new Date().toISOString(),
  status: 'COMPLETE', fleet_status: 'FRESH', issues: [], zones: ['CBD', 'Bishan', 'Tampines', 'Punggol', 'Woodlands', 'Jurong East'].map((name, i) => ({
    zone_id: name.toLowerCase().replaceAll(' ', '-'), name, position: { latitude: 1.28 + i * .02, longitude: 103.85 },
    rental_demand: 10, demand_level: 'HIGH', demand_status: 'FRESH', available_evs: 0, inbound_relocating_evs: 0,
    inbound_pending_evs: 0, inbound_unconfirmed_evs: 0, current_shortage: 10, projected_shortage: 10,
    current_surplus: 0, projected_surplus: 0, hotspot_severity: i ? 'NORMAL' : 'CRITICAL', data_quality: 'COMPLETE',
  })),
});
export const makeFleet = (): Fleet => ({ instance_id: 'fleet-instance', snapshot_at: new Date().toISOString(), vehicles: [{
  vehicle_id: 'ev-01', state_version: 1, current_zone_id: 'bishan', stale: false, connectivity: 'ONLINE', effective_availability: 'AVAILABLE',
  effective_relocation_state: 'IDLE', received_at: new Date().toISOString(),
  telemetry: { position: { latitude: 1.35, longitude: 103.85 }, soc_pct: 90, observed_at: new Date().toISOString() },
}] });
export const makeRecommendation = (): Recommendation => ({
  recommendation_id: 'rec-1', created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 10000).toISOString(),
  selected_vehicle_id: 'ev-01', target: { zone_id: 'cbd', name: 'CBD' }, explanation: 'Backend selected the highest scoring eligible EV.', assumptions: ['Simulated charging only'],
  ranked_candidates: [{ vehicle_id: 'ev-01', current_soc_pct: 90, predicted_arrival_soc_pct: 85, distance_km: 8,
    energy_required_kwh: 2, score: .8, charging_assessment: 'AVAILABLE', exclusion_reasons: [] }], excluded_candidates: [],
});
export const makeDispatch = (): Dispatch => ({
  dispatch_id: 'dispatch-1', request_id: 'approval-1', vehicle_id: 'ev-01', state: 'IN_PROGRESS',
  command: { target_zone_id: 'cbd', target_position: { latitude: 1.283, longitude: 103.85 } },
  created_at: new Date().toISOString(), acknowledged_at: new Date().toISOString(), last_contact_at: new Date().toISOString(),
  distance_travelled_km: 2, arrival_soc_pct: null, failure_reason: null, claim_held: true, revalidated_metrics: { distance_km: 8 },
});
