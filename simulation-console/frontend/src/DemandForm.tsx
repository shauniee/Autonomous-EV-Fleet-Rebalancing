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
  const [selected, setSelected] = useState('');
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const [result, setResult] = useState<{
    message: string;
    success: boolean;
  } | null>(null);
  const zone = zones.find((item) => item.zone_id === selected);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!available || !zone || sending.current) return;
    const desired = Number(value);
    if (
      !value.trim() ||
      !Number.isInteger(desired) ||
      desired < 0 ||
      desired > 10000
    ) {
      setResult({
        message: 'Enter a whole number from 0 to 10000.',
        success: false,
      });
      return;
    }
    sending.current = true;
    setBusy(true);
    setResult(null);
    try {
      const response = await fetch(
        `/api/probe/v1/zones/${zone.zone_id}/demand`,
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
        setResult({
          success: false,
          message: data.detail?.outcome_uncertain
            ? `${message} It may have applied. Check current demand before submitting again.`
            : message,
        });
        return;
      }
      setResult({
        success: true,
        message: `Applied desired supply of ${desired} to ${zone.name}. Updated observations appear on the next poll.`,
      });
    } catch {
      setResult({
        success: false,
        message:
          'Submission outcome uncertain. It may have applied. Check current demand before submitting again.',
      });
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  return (
    <section>
      <h2>Set zone demand</h2>
      <p className="description">
        Demand is desired available vehicles, not API traffic or real rentals.
        This input does not create vehicles or approve relocations.
      </p>
      <form className="demand-form" onSubmit={submit}>
        <label>
          Zone
          <select
            required
            value={selected}
            disabled={busy || !available}
            onChange={(event) => {
              setSelected(event.target.value);
              setResult(null);
            }}
          >
            <option value="">Select a zone</option>
            {zones.map((item) => (
              <option key={item.zone_id} value={item.zone_id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <p>
          Current desired supply:{' '}
          <strong>
            {zone?.demand?.desired_available_vehicles ?? 'Unknown'}
          </strong>
        </p>
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
              setResult(null);
            }}
          />
        </label>
        <button type="submit" disabled={busy || !available || !zone}>
          {busy ? 'Applying…' : 'Apply demand'}
        </button>
      </form>
      {!available && (
        <p className="warning">
          Fresh zone observations are required before submitting demand.
        </p>
      )}
      {result && (
        <p
          className={result.success ? 'success' : 'warning'}
          role={result.success ? 'status' : 'alert'}
        >
          {result.message}
        </p>
      )}
      <p className="description">
        A new timestamp is sent only when you apply. Demand can expire, and the
        app’s demand simulator may replace it after its manual hold. Inputs are
        never reapplied automatically.
      </p>
    </section>
  );
}
