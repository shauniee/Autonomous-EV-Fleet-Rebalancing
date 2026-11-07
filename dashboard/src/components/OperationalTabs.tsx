import { useEffect, useId, useRef, useState, type ReactNode } from 'react';

export function OperationalTabs({ tabs, activeTab, onTabChange, label = 'Operational views' }: { label?: string; activeTab?: string; onTabChange?: (id: string) => void; tabs: { id: string; label: string; summary?: string; content: ReactNode }[] }) {
  const [localActive, setLocalActive] = useState(tabs[0]?.id);
  const active = activeTab ?? localActive;
  const setActive = (id: string) => { setLocalActive(id); onTabChange?.(id); };
  const prefix = useId();
  const tabList = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const list = tabList.current;
    const tab = document.getElementById(`${prefix}-${active}-tab`);
    if (!list || !tab) return;
    const frame = list.getBoundingClientRect();
    const button = tab.getBoundingClientRect();
    if (button.left < frame.left) list.scrollLeft += button.left - frame.left;
    if (button.right > frame.right) list.scrollLeft += button.right - frame.right;
  }, [active, prefix]);
  return <div className="operational-tabs">
    <div ref={tabList} className="operational-tablist" role="tablist" aria-label={label}>
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
