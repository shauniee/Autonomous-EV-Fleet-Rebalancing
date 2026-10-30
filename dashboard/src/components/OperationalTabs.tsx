import { useId, useState, type ReactNode } from 'react';

export function OperationalTabs({ tabs }: { tabs: { id: string; label: string; summary?: string; content: ReactNode }[] }) {
  const [active, setActive] = useState('fleet');
  const prefix = useId();
  return <div className="operational-tabs">
    <div className="operational-tablist" role="tablist" aria-label="Operational views">
      {tabs.map((tab, index) => <button key={tab.id} id={`${prefix}-${tab.id}-tab`} type="button" role="tab"
        aria-selected={active === tab.id} aria-controls={`${prefix}-${tab.id}-panel`} tabIndex={active === tab.id ? 0 : -1}
        onClick={() => setActive(tab.id)} onKeyDown={event => {
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
            : event.key === 'ArrowRight' ? (index + 1) % tabs.length
            : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : -1;
          if (next < 0) return;
          event.preventDefault();
          setActive(tabs[next].id);
          document.getElementById(`${prefix}-${tabs[next].id}-tab`)?.focus();
        }}>
        {tab.label}{tab.summary && <span className="tab-count">{tab.summary}</span>}
      </button>)}
    </div>
    {tabs.map(tab => <div key={tab.id} id={`${prefix}-${tab.id}-panel`} className="operational-panel" role="tabpanel"
      aria-labelledby={`${prefix}-${tab.id}-tab`} hidden={active !== tab.id} tabIndex={0}>
      {tab.content}
    </div>)}
  </div>;
}
