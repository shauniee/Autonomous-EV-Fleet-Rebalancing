import { render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { demandLevel } from '../src/components/DemandThresholds';
import { FleetPulse } from '../src/components/FleetPulse';
import { makeDispatch, makeFleet, makeHeat } from './fixtures';
import type { Zone } from '../src/api/contracts';

const classifyDemand = (zone: Zone) => demandLevel(zone, { moderate: 5, critical: 10 });

function metric(label: string) {
  const heading = screen.getByText(label);
  const container = heading.closest('.fleet-pulse-metric');
  if (!container) throw new Error(`Missing metric ${label}`);
  return within(container as HTMLElement);
}

describe('operational dashboard snapshot', () => {
  afterEach(() => vi.useRealTimers());

  it('keeps confirmed values when a poll arrives between dashboard clock ticks', () => {
    const now = Date.now();
    const updatedAt = new Date(now).toISOString();
    const props = {
      fleet: { data: makeFleet(), updatedAt },
      heat: { data: makeHeat(), updatedAt },
      dispatches: { data: { dispatches: [makeDispatch()] }, updatedAt },
      rentalDemandLevel,
      now,
    };
    const view = render(<FleetPulse {...props}/>);

    const nextPoll = new Date(now + 100).toISOString();
    view.rerender(<FleetPulse {...props}
      fleet={{ ...props.fleet, updatedAt: nextPoll }}
      heat={{ ...props.heat, updatedAt: nextPoll }}
      dispatches={{ ...props.dispatches, updatedAt: nextPoll }}/>);

    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Available EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Open dispatches').getByText('1')).toBeInTheDocument();
    expect(metric('Critical zones').getByText('6')).toBeInTheDocument();
  });

  it('retains missing service values for less than five minutes without renewing their age', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-08T04:00:00Z'));
    const updatedAt = new Date().toISOString();
    const view = render(<FleetPulse
      fleet={{ data: makeFleet(), updatedAt }}
      heat={{ data: makeHeat(), updatedAt }}
      dispatches={{ data: { dispatches: [makeDispatch()] }, updatedAt }}
      rentalDemandLevel={rentalDemandLevel} now={Date.now()}/>);
    const missingSnapshot = () => <FleetPulse
      fleet={{ error: 'Connection interrupted' }}
      heat={{ error: 'Connection interrupted' }}
      dispatches={{ error: 'Connection interrupted' }}
      rentalDemandLevel={rentalDemandLevel} now={Date.now()}/>;

    vi.advanceTimersByTime(1000);
    view.rerender(missingSnapshot());
    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Available EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Open dispatches').getByText('1')).toBeInTheDocument();
    expect(metric('Critical zones').getByText('6')).toBeInTheDocument();
    expect(screen.getAllByText('Last confirmed observation')).toHaveLength(4);

    vi.advanceTimersByTime(298999);
    view.rerender(missingSnapshot());
    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Critical zones').getByText('6')).toBeInTheDocument();

    vi.advanceTimersByTime(1);
    view.rerender(missingSnapshot());
    for (const label of ['Reporting EVs', 'Available EVs', 'Open dispatches', 'Critical zones']) {
      expect(metric(label).getByLabelText(`${label}: unavailable`)).toHaveTextContent('—');
    }
  });

  it('retains partial or unknown demand independently while accepting fresh fleet zeros', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-08T04:00:00Z'));
    const updatedAt = new Date().toISOString();
    const view = render(<FleetPulse
      fleet={{ data: makeFleet(), updatedAt }}
      heat={{ data: makeHeat(), updatedAt }}
      dispatches={{ data: { dispatches: [makeDispatch()] }, updatedAt }}
      rentalDemandLevel={rentalDemandLevel} now={Date.now()}/>);
    vi.advanceTimersByTime(120000);
    const fresh = new Date().toISOString();
    const emptyFleet = { ...makeFleet(), vehicles: [] };
    const partialHeat = { ...makeHeat(), status: 'PARTIAL' };
    const props = {
      fleet: { data: emptyFleet, updatedAt: fresh },
      heat: { data: partialHeat, updatedAt: fresh },
      dispatches: { error: 'Connection interrupted' },
      rentalDemandLevel,
      now: Date.now(),
    };
    view.rerender(<FleetPulse {...props}/>);
    expect(metric('Reporting EVs').getByText('0')).toBeInTheDocument();
    expect(metric('Available EVs').getByText('0')).toBeInTheDocument();
    expect(metric('Critical zones').getByText('6')).toBeInTheDocument();
    expect(metric('Critical zones').getByText('Last confirmed observation')).toBeInTheDocument();

    vi.advanceTimersByTime(179999);
    const unknownHeat = makeHeat();
    unknownHeat.zones[0].demand_status = 'STALE';
    view.rerender(<FleetPulse {...props}
      heat={{ data: unknownHeat, updatedAt: new Date().toISOString() }} now={Date.now()}/>);
    expect(metric('Critical zones').getByText('6')).toBeInTheDocument();

    vi.advanceTimersByTime(1);
    view.rerender(<FleetPulse {...props} now={Date.now()}/>);
    expect(metric('Critical zones').getByLabelText('Critical zones: unavailable')).toHaveTextContent('—');
    expect(metric('Open dispatches').getByLabelText('Open dispatches: unavailable')).toHaveTextContent('—');
    expect(metric('Reporting EVs').getByText('0')).toBeInTheDocument();
    expect(metric('Available EVs').getByText('0')).toBeInTheDocument();
  });

  it('shows only observed counts and includes unresolved dispatches', () => {
    const now = Date.now();
    const updatedAt = new Date(now).toISOString();
    const fleet = makeFleet();
    const heat = makeHeat();
    const unresolved = makeDispatch();
    unresolved.state = 'UNKNOWN';
    const completed = { ...makeDispatch(), dispatch_id: 'completed', state: 'SUCCEEDED' };

    render(<FleetPulse
      fleet={{ data: fleet, updatedAt }}
      heat={{ data: heat, updatedAt }}
      dispatches={{ data: { dispatches: [unresolved, completed] }, updatedAt }}
      rentalDemandLevel={classifyDemand}
      now={now}
    />);

    expect(screen.getByRole('heading', { name: 'Operational snapshot' })).toBeInTheDocument();
    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Available EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Open dispatches').getByText('1')).toBeInTheDocument();
    expect(metric('Critical zones').getByText('6')).toBeInTheDocument();
  });

  it('does not turn stale, unavailable or partial service data into zero counts', () => {
    const now = Date.now();
    const old = new Date(now - 300000).toISOString();
    const fresh = new Date(now).toISOString();
    const heat = makeHeat();
    heat.status = 'PARTIAL';
    render(<FleetPulse
      fleet={{ data: makeFleet(), updatedAt: old }}
      heat={{ data: heat, updatedAt: fresh }}
      dispatches={{ data: { dispatches: [] }, error: 'Connection lost', updatedAt: old }}
      rentalDemandLevel={classifyDemand}
      now={now}
    />);
    expect(metric('Reporting EVs').getByLabelText('Reporting EVs: unavailable')).toHaveTextContent('—');
    expect(metric('Available EVs').getByLabelText('Available EVs: unavailable')).toHaveTextContent('—');
    expect(metric('Critical zones').getByLabelText('Critical zones: unavailable')).toHaveTextContent('—');
    expect(metric('Open dispatches').getByLabelText('Open dispatches: unavailable')).toHaveTextContent('—');
    expect(screen.getAllByText('Awaiting current data')).toHaveLength(4);
  });

  it('keeps recent confirmed counts visible during a transient poll error', () => {
    const now = Date.now();
    const updatedAt = new Date(now - 299999).toISOString();

    render(<FleetPulse
      fleet={{ data: makeFleet(), error: 'Connection interrupted', updatedAt }}
      heat={{ data: makeHeat(), error: 'Connection interrupted', updatedAt }}
      dispatches={{ data: { dispatches: [makeDispatch()] }, error: 'Connection interrupted', updatedAt }}
      rentalDemandLevel={classifyDemand}
      now={now}
    />);

    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Available EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Open dispatches').getByText('1')).toBeInTheDocument();
    expect(metric('Critical zones').getByText('6')).toBeInTheDocument();
    expect(screen.getAllByText('Last confirmed observation')).toHaveLength(4);
  });

  it('replaces retained counts as soon as a newer valid observation arrives', () => {
    const now = Date.now();
    const old = new Date(now - 120000).toISOString();
    const fresh = new Date(now).toISOString();
    const initialFleet = makeFleet();
    const updatedFleet = makeFleet();
    updatedFleet.vehicles = [];

    const view = render(<FleetPulse
      fleet={{ data: initialFleet, error: 'Connection interrupted', updatedAt: old }}
      heat={{ data: makeHeat(), updatedAt: fresh }}
      dispatches={{ data: { dispatches: [] }, updatedAt: fresh }}
      rentalDemandLevel={classifyDemand}
      now={now}
    />);
    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();

    view.rerender(<FleetPulse
      fleet={{ data: updatedFleet, updatedAt: fresh }}
      heat={{ data: makeHeat(), updatedAt: fresh }}
      dispatches={{ data: { dispatches: [] }, updatedAt: fresh }}
      rentalDemandLevel={classifyDemand}
      now={now}
    />);
    expect(metric('Reporting EVs').getByText('0')).toBeInTheDocument();
  });

  it('shows real zeros for fresh empty service snapshots', () => {
    const now = Date.now();
    const updatedAt = new Date(now).toISOString();
    const fleet = makeFleet();
    fleet.vehicles = [];
    const heat = makeHeat();
    heat.zones = [];
    render(<FleetPulse
      fleet={{ data: fleet, updatedAt }}
      heat={{ data: heat, updatedAt }}
      dispatches={{ data: { dispatches: [] }, updatedAt }}
      rentalDemandLevel={classifyDemand}
      now={now}
    />);
    for (const label of ['Reporting EVs', 'Available EVs', 'Open dispatches', 'Critical zones']) {
      expect(metric(label).getByText('0')).toBeInTheDocument();
    }
  });
});
