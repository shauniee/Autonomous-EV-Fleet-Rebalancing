import type { Fleet } from '../api/contracts';

export function RecommendationVehiclePicker({ vehicles, selected, onChange, disabled }: {
  vehicles: Fleet['vehicles'];
  selected: string[];
  onChange: (ids: string[]) => void;
  disabled: boolean;
}) {
  const fleet = new Map(vehicles.map(vehicle => [vehicle.vehicle_id, vehicle]));
  const ids = [...new Set([...selected, ...fleet.keys()])].sort();
  return <fieldset className="recommendation-vehicle-picker" disabled={disabled}>
    <legend>Specific EVs</legend>
    <p id="specific-ev-help" className="muted">Choose up to 20 EVs. CRDS may recommend fewer.</p>
    {!ids.length && <p className="muted">Waiting for fleet data.</p>}
    <div className="recommendation-vehicle-options">
      {ids.map(id => {
        const vehicle = fleet.get(id);
        const checked = selected.includes(id);
        const status = !vehicle ? 'Missing from fleet' : vehicle.stale ? 'Telemetry stale'
          : vehicle.effective_availability === 'AVAILABLE' ? 'Available' : 'Unavailable';
        return <label key={id}>
          <input type="checkbox" checked={checked} aria-describedby="specific-ev-help"
            disabled={!checked && (status !== 'Available' || selected.length >= 20)}
            onChange={event => onChange(event.target.checked ? [...selected, id].sort()
              : selected.filter(value => value !== id))}/>
          <span><strong>{id}</strong><span className="muted"> {status}
            {vehicle?.telemetry ? ` · ${vehicle.telemetry.soc_pct.toFixed(1)}% battery` : ''}</span></span>
        </label>;
      })}
    </div>
  </fieldset>;
}
