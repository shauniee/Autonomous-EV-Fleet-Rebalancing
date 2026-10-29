import { useCallback, useEffect, useRef, useState } from 'react';
import type { Dispatch, HeatMap } from '../api/contracts';

type Update = { id: number; title: string; detail: string };
const stateLabels: Record<string, string> = {
  SENT: 'Sent to relocate', ACKNOWLEDGED: 'Relocation accepted',
  IN_PROGRESS: 'Relocating', SUCCEEDED: 'Relocation completed',
  FAILED: 'Relocation failed', REJECTED: 'Relocation rejected',
};

function Toast({ update, dismiss }: { update: Update; dismiss: (id: number) => void }) {
  useEffect(() => {
    const timer = window.setTimeout(() => dismiss(update.id), 8000);
    return () => window.clearTimeout(timer);
  }, [update.id, dismiss]);
  return <div className="activity-toast"><div><strong>{update.title}</strong><p>{update.detail}</p></div>
    <button aria-label={`Dismiss ${update.title}`} onClick={() => dismiss(update.id)}>×</button></div>;
}

export function ActivityToasts({ zones, dispatches }: { zones?: HeatMap['zones']; dispatches?: Dispatch[] }) {
  const previousZones = useRef(new Map<string, string>());
  const previousDispatches = useRef<Map<string, string> | null>(null);
  const nextId = useRef(0);
  const [updates, setUpdates] = useState<Update[]>([]);
  const [history, setHistory] = useState<Update[]>([]);
  const [open, setOpen] = useState(false);
  const [readThrough, setReadThrough] = useState(-1);
  const panel = useRef<HTMLDivElement>(null);
  const record = useCallback((changes: Update[]) => {
    if (!changes.length) return;
    setUpdates(items => [...items, ...changes].slice(-6));
    setHistory(items => [...changes].reverse().concat(items).slice(0, 50));
  }, []);
  useEffect(() => {
    if (!open) return;
    setReadThrough(nextId.current - 1);
    function outside(event: PointerEvent) {
      if (!panel.current?.contains(event.target as Node)) setOpen(false);
    }
    function escape(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setOpen(false);
        panel.current?.querySelector('button')?.focus();
      }
    }
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open, history]);
  const dismiss = useCallback((id: number) => setUpdates(items => items.filter(item => item.id !== id)), []);
  useEffect(() => {
    if (!zones) return;
    const changes: Update[] = [];
    for (const zone of zones) {
      const before = previousZones.current.get(zone.zone_id);
      if (before !== undefined && before !== zone.hotspot_severity) {
        changes.push({ id: nextId.current++, title: `${zone.name} updated`, detail: `Hotspot severity: ${before} → ${zone.hotspot_severity}` });
      }
      previousZones.current.set(zone.zone_id, zone.hotspot_severity);
    }
    record(changes);
  }, [zones, record]);
  useEffect(() => {
    if (!dispatches) return;
    const changes: Update[] = [];
    const previous = previousDispatches.current;
    for (const dispatch of dispatches) {
      if (previous && previous.get(dispatch.dispatch_id) !== dispatch.state) {
        const destination = dispatch.command?.target_zone_id;
        const area = zones?.find(zone => zone.zone_id === destination)?.name ?? destination;
        changes.push({ id: nextId.current++, title: `${dispatch.vehicle_id ?? 'Vehicle'} · ${stateLabels[dispatch.state] ?? dispatch.state.replaceAll('_', ' ').toLowerCase()}`,
          detail: [area ? `Destination: ${area}` : '', dispatch.failure_reason].filter(Boolean).join(' · ') || 'Dispatch status updated.' });
      }
    }
    // Keep known dispatches so a temporarily omitted record cannot repeat its notification.
    const current = new Map(previous);
    dispatches.forEach(dispatch => current.set(dispatch.dispatch_id, dispatch.state));
    previousDispatches.current = current;
    record(changes);
  }, [dispatches, zones, record]);
  const unread = history.filter(update => update.id > readThrough).length;
  return <><div className="notification-control" ref={panel}>
    <button className="notification-button" aria-label={`Notifications${unread ? `, ${unread} unread` : ''}`} aria-expanded={open} aria-controls="notification-history" onClick={() => setOpen(value => !value)}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Z"/><path d="M10 21h4"/></svg>
      Notifications {unread > 0 && <span className="notification-count">{unread}</span>}
    </button>
    {open && <section id="notification-history" className="notification-history" aria-label="Recent notifications">
      <div className="notification-heading"><strong>Notifications</strong>{history.length > 0 && <button onClick={() => setHistory([])}>Clear all</button>}</div>
      {history.length ? <ul>{history.map(update => <li key={update.id}><strong>{update.title}</strong><p>{update.detail}</p></li>)}</ul>
        : <p className="notification-empty">No notifications yet. Hotspot and car updates will appear here.</p>}
    </section>}
  </div><div className="activity-toasts" role="status" aria-live="polite" aria-relevant="additions" aria-label="Fleet updates">
    {updates.map(update => <Toast key={update.id} update={update} dismiss={dismiss}/>)}
  </div></>;
}
