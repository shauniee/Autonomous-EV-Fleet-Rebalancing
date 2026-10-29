import { useEffect, useRef, useState } from 'react';
import type { HeatMap } from '../api/contracts';
import { Badge } from './format';

type Zone = HeatMap['zones'][number];
type Update = { id: number; name: string; before: Zone['hotspot_severity']; after: Zone['hotspot_severity'] };

function Toast({ update, dismiss }: { update: Update; dismiss: (id: number) => void }) {
  useEffect(() => {
    const timer = window.setTimeout(() => dismiss(update.id), 8000);
    return () => window.clearTimeout(timer);
  }, [update.id, dismiss]);
  return <div className="hotspot-toast">
    <div><strong>{update.name} updated</strong><p>Hotspot severity: <Badge value={update.before}/> → <Badge value={update.after}/></p></div>
    <button aria-label={`Dismiss ${update.name} update`} onClick={() => dismiss(update.id)}>×</button>
  </div>;
}

export function HotspotToasts({ zones }: { zones?: Zone[] }) {
  const previous = useRef(new Map<string, Zone['hotspot_severity']>());
  const nextId = useRef(0);
  const [updates, setUpdates] = useState<Update[]>([]);
  const dismiss = useRef((id: number) => setUpdates(items => items.filter(item => item.id !== id))).current;
  useEffect(() => {
    if (!zones) return;
    const changes: Update[] = [];
    for (const zone of zones) {
      const before = previous.current.get(zone.zone_id);
      if (before !== undefined && before !== zone.hotspot_severity) {
        changes.push({ id: nextId.current++, name: zone.name, before, after: zone.hotspot_severity });
      }
    }
    previous.current = new Map(zones.map(zone => [zone.zone_id, zone.hotspot_severity]));
    if (changes.length) setUpdates(items => [...items, ...changes].slice(-6));
  }, [zones]);
  return <div className="hotspot-toasts" role="status" aria-live="polite" aria-relevant="additions" aria-label="Hotspot updates">
    {updates.map(update => <Toast key={update.id} update={update} dismiss={dismiss}/>)}
  </div>;
}
