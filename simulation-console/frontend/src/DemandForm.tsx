import { useRef, useState } from 'react';
import type { FormEvent } from 'react';

export type Zone = {
  zone_id: string;
  name: string;
  demand: { desired_available_vehicles: number; observed_at: string } | null;
};

export function DemandForm({
  zones,
  available,
}: {
  zones: Zone[];
  available: boolean;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const [results, setResults] = useState<{
    zoneId: string;
    message: string;
    success: boolean;
  }[]>([]);
  const [validation, setValidation] = useState('');
  const selectedZones = zones.filter((item) => selected.includes(item.zone_id));

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!available || !selectedZones.length || sending.current) return;
    const desired = Number(value);
    if (
      !value.trim() ||
      !Number.isInteger(desired) ||
      desired < 0 ||
      desired > 10000
    ) {
      setValidation('Enter a whole number from 0 to 10000.');
      return;
    }
    sending.current = true;
    setBusy(true);
    setResults([]);
    setValidation('');
    // Snapshot the selection; polling must not change an in-progress submission.
    try {
      for (const zone of selectedZones) {
        let result: { message: string; success: boolean };
        try {
          const response = await fetch(
            `/api/probe/v1/zones/${encodeURIComponent(zone.zone_id)}/demand`,
            {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ desired_available_vehicles: desired }),
              signal: AbortSignal.timeout(8000),
            },
          );
          const data = await response.json();
          if (!response.ok) {
            const message =
              typeof data.detail?.message === 'string'
                ? data.detail.message
                : 'The demand input was rejected.';
            result = {
              success: false,
              message: data.detail?.outcome_uncertain
                ? `${message} It may have applied. Check current demand before submitting again.`
                : message,
            };
          } else {
            result = {
              success: true,
              message: `Applied desired supply of ${desired} to ${zone.name}. Updated observations appear on the next poll.`,
            };
          }
        } catch {
          result = {
            success: false,
            message:
              'Submission outcome uncertain. It may have applied. Check current demand before submitting again.',
          };
        }
        // Independent writes: keep successes and never retry uncertain results.
        setResults((previous) => [...previous, { ...result, zoneId: zone.zone_id }]);
      }
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  return (
    <section>
      <h2>Set zone demand</h2>
      <p className="description">
        Set a target for available vehicles in each selected zone.
      </p>
      <p className="description">
        For a relocation demonstration, give every zone a fresh demand value first:
        select all zones and apply 0, then select the high-demand zone and apply a larger target.
        CRDS assesses fleet-wide shortages, so missing or expired demand in another zone can
        prevent any recommendation. Pause automatic demand updates while entering your scenario;
        refresh manual observations before they expire.
      </p>
      <form className="demand-layout" onSubmit={submit}>
        <fieldset className="demand-zones" disabled={busy || !available}>
          <legend>Zones ({selectedZones.length} selected)</legend>
          <div className="demand-selection-actions">
            <button type="button" onClick={() => {
              setSelected(zones.map((item) => item.zone_id));
              setResults([]);
            }}>Select all</button>
            <button type="button" onClick={() => {
              setSelected([]);
              setResults([]);
            }}>Clear selection</button>
          </div>
          <div className="zone-picker">
            {zones.map((item) => (
              <label key={item.zone_id}>
                <input type="checkbox" checked={selected.includes(item.zone_id)}
                  onChange={(event) => {
                    setSelected((previous) => event.target.checked
                      ? [...previous, item.zone_id]
                      : previous.filter((id) => id !== item.zone_id));
                    setResults([]);
                  }} />
                <span>{item.name}<small>Current desired supply: {item.demand?.desired_available_vehicles ?? 'Unknown'}</small></span>
              </label>
            ))}
            {!zones.length && <p>No zones observed.</p>}
          </div>
        </fieldset>
        <div className="demand-settings">
          <label>
            Desired available vehicles
            <input
              type="number"
              min="0"
              max="10000"
              step="1"
              required
              value={value}
              disabled={busy || !available}
              onChange={(event) => {
                setValue(event.target.value);
                setResults([]);
                setValidation('');
              }}
            />
          </label>
          <button className="primary-action" type="submit" disabled={busy || !available || !selectedZones.length}>
            {busy ? `Applying… (${results.length}/${selectedZones.length})` : `Apply demand to ${selectedZones.length} selected zone${selectedZones.length === 1 ? '' : 's'}`}
          </button>
          <p className="description">Updates apply independently. Successful changes remain applied if another zone fails.</p>
          {!available && (
            <p className="warning">
              Fresh zone observations are required before submitting demand.
            </p>
          )}
          {validation && <p className="warning" role="alert">{validation}</p>}
          <div className="demand-results" aria-live="polite" aria-busy={busy}
            tabIndex={results.length ? 0 : undefined}>
            {results.map((result) => (
              <p key={result.zoneId} className={result.success ? 'success' : 'warning'}>
                <strong>{zones.find((item) => item.zone_id === result.zoneId)?.name ?? result.zoneId}: </strong>
                {result.message}
              </p>
            ))}
          </div>
          <details className="help">
            <summary>How demand updates work</summary>
            <p className="description">
              This input sets a supply target without generating API traffic, creating vehicles,
              starting rentals, or approving relocations. Manual targets stay valid while demand simulation is paused.
              When it runs, the simulator can replace a target after its manual hold. Generated demand can expire.
            </p>
          </details>
        </div>
      </form>
    </section>
  );
}
