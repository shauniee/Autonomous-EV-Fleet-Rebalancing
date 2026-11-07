import { useRef, useState } from 'react';
import type { HeatMap, Zone } from '../api/contracts';

export const demandLevels = ['Low', 'Moderate', 'Critical', 'Unknown'] as const;
export type DemandLevel = typeof demandLevels[number];

export interface Thresholds { moderate: number; critical: number }

const storageKey = 'fleet.demand-thresholds';

function valid({ moderate, critical }: Thresholds) {
  return Number.isSafeInteger(moderate) && Number.isSafeInteger(critical) && moderate >= 1 && critical > moderate;
}

export function defaultDemandThresholds(policy: HeatMap['policy']): Thresholds {
  const thresholds = { moderate: policy?.medium_demand ?? 5, critical: policy?.high_demand ?? 10 };
  return valid(thresholds) ? thresholds : { moderate: 5, critical: 10 };
}

export function readDemandThresholds(): Thresholds | null {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) ?? 'null');
    return value && valid(value) ? value : null;
  } catch {
    return null;
  }
}

export function demandLevel(zone: Zone, thresholds: Thresholds): DemandLevel {
  const demand = zone.rental_demand;
  if (zone.demand_status !== 'FRESH' || demand == null) return 'Unknown';
  if (demand >= thresholds.critical) return 'Critical';
  return demand >= thresholds.moderate ? 'Moderate' : 'Low';
}

function Ranges({ thresholds }: { thresholds: Thresholds }) {
  const { moderate, critical } = thresholds;
  return <ul className="demand-ranges" aria-label="Rental demand ranges in EVs">
    <li><span className="badge low">Low</span> {moderate === 1 ? '0' : `0–${moderate - 1}`}</li>
    <li><span className="badge moderate">Moderate</span> {critical === moderate + 1 ? moderate : `${moderate}–${critical - 1}`}</li>
    <li><span className="badge critical">Critical</span> {critical}+</li>
  </ul>;
}

export function DemandThresholds({ applied, defaults, onApply }: {
  applied: Thresholds; defaults: Thresholds; onApply: (value: Thresholds) => void;
}) {
  const [draft, setDraft] = useState<{ moderate: string; critical: string } | null>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  const [feedback, setFeedback] = useState('');
  const values = draft ? { moderate: Number(draft.moderate), critical: Number(draft.critical) } : applied;
  const isValid = valid(values);
  const edit = (values: Thresholds) => setDraft({ moderate: String(values.moderate), critical: String(values.critical) });

  return <section className="demand-thresholds" aria-label="Rental demand thresholds">
    <div className="demand-threshold-heading">
      <h3>Rental demand thresholds</h3>
      <button type="button" ref={editButton} aria-expanded={!!draft} onClick={() => {
        setFeedback('');
        if (draft) setDraft(null);
        else edit(applied);
      }}>{draft ? 'Cancel' : 'Edit thresholds'}</button>
    </div>
    {draft ? <form noValidate onSubmit={event => {
      event.preventDefault();
      if (!isValid) return;
      onApply(values);
      setDraft(null);
      editButton.current?.focus();
      try {
        localStorage.setItem(storageKey, JSON.stringify(values));
        setFeedback('Thresholds saved in this browser.');
      } catch {
        setFeedback('Thresholds applied for this visit. Browser storage is unavailable.');
      }
    }}>
      <p id="demand-threshold-help" className="muted">Enter the minimum number of EVs requested per zone for each level. Counts below Moderate are Low. These settings apply to the dashboard in this browser.</p>
      <div className="demand-threshold-fields">
        {(['moderate', 'critical'] as const).map(level => <label key={level}>
          {level === 'moderate' ? 'Moderate starts at' : 'Critical starts at'}
          <input type="number" inputMode="numeric" min="1" step="1" value={draft[level]}
            aria-describedby={isValid ? 'demand-threshold-help' : 'demand-threshold-help demand-threshold-error'}
            onChange={event => setDraft({ ...draft, [level]: event.target.value })}/>
        </label>)}
      </div>
      {isValid ? <Ranges thresholds={values}/> :
        <p id="demand-threshold-error" className="demand-threshold-error">Use positive whole numbers. Critical must be higher than Moderate.</p>}
      <div className="demand-threshold-actions">
        <button type="button" onClick={() => edit(defaults)}>Use defaults</button>
        <button type="submit" className="primary" disabled={!isValid}>Apply thresholds</button>
      </div>
    </form> : <Ranges thresholds={applied}/>}
    <div className="muted" role="status">{feedback}</div>
  </section>;
}
