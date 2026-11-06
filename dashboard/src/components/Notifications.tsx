import { useEffect, useRef, useState, type ReactNode } from 'react';
import { time } from './format';

type NotificationTone = 'error' | 'warning' | 'info';

export type Notify = (tone: NotificationTone, message: string) => void;

export type Notification = {
  id: number;
  tone: NotificationTone;
  message: string;
  createdAt: string;
};

export type ActiveNotification = {
  id: string;
  title: string;
  message: string;
  actions?: ReactNode;
};

const toneLabel: Record<NotificationTone, string> = {
  error: 'Error',
  warning: 'Warning',
  info: 'Update',
};

export function Notifications({ open, onOpenChange, active, history, onClear }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  active: ActiveNotification[];
  history: Notification[];
  onClear: () => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [seen, setSeen] = useState(0);
  const latest = history[0];
  const latestId = latest?.id ?? 0;
  const unread = history.filter(item => item.id > seen).length;

  useEffect(() => {
    if (!open) return;
    setSeen(value => Math.max(value, latestId));
  }, [open, latestId]);

  useEffect(() => {
    if (!open) return;
    function closeOutside(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) onOpenChange(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('dialog[open]')) return;
      event.preventDefault();
      onOpenChange(false);
      trigger.current?.focus();
    }
    document.addEventListener('pointerdown', closeOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('pointerdown', closeOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [open, onOpenChange]);

  const count = active.length + unread;
  return <div className="notifications" ref={root}>
    <button type="button" className="notification-trigger" ref={trigger} aria-expanded={open}
      aria-controls="dispatch-notifications" aria-label={count ? `Notifications, ${count} to review` : 'Notifications'}
      onClick={() => onOpenChange(!open)}>
      <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>
      </svg>
      <span className="notification-trigger-label">Notifications</span>
      {count > 0 && <span className="notification-count" data-active={active.length > 0} aria-hidden="true">{count}</span>}
    </button>
    <span className="visually-hidden" role="status" aria-atomic="true">
      {active.map(item => item.title).join('. ')}{latest && <> {latest.message}</>}
    </span>
    <section id="dispatch-notifications" className="notification-panel" aria-labelledby="notifications-title" hidden={!open}>
      <div className="notification-heading">
        <h2 id="notifications-title" tabIndex={-1}>Notifications</h2>
        <button type="button" onClick={() => { onOpenChange(false); trigger.current?.focus(); }}>Close</button>
      </div>
      <div className="notification-body">
        {active.length > 0 && <div className="notification-group">
          <h3>Active now</h3>
          <ul>{active.map(item => <li key={item.id}>
            <strong>{item.title}</strong>
            <p>{item.message}</p>
            {item.actions && <div className="notification-actions">{item.actions}</div>}
          </li>)}</ul>
        </div>}
        <div className="notification-group">
          <div className="notification-group-heading">
            <h3>Recent activity</h3>
            {history.length > 0 && <button className="text-button" type="button" onClick={() => {
              onClear();
              trigger.current?.focus();
            }}>Clear activity</button>}
          </div>
          {history.length > 0 ? <ul>{history.map(item => <li key={item.id}>
            <div className="notification-item-heading">
              <strong data-tone={item.tone}>{toneLabel[item.tone]}</strong>
              <time dateTime={item.createdAt}>{time(item.createdAt)} SGT</time>
            </div>
            <p>{item.message}</p>
          </li>)}</ul> : <p className="muted">No recent activity.</p>}
        </div>
      </div>
    </section>
  </div>;
}
