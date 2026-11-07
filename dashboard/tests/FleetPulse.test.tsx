import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { demandLevel } from '../src/components/DemandThresholds';
import { FleetPulse } from '../src/components/FleetPulse';
import { makeDispatch, makeFleet, makeHeat } from './fixtures';

function metric(label: string) {
  const heading = screen.getByText(label);
  const container = heading.closest('.fleet-pulse-metric');
  if (!container) throw new Error(`Missing metric ${label}`);
  return within(container as HTMLElement);
}

describe('operational dashboard snapshot', () => {
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
      rentalDemandLevel={zone => demandLevel(zone, { moderate: 5, critical: 10 })}
      now={now}
    />);

    expect(screen.getByRole('heading', { name: 'Operational snapshot' })).toBeInTheDocument();
    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Available EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Open dispatches').getByText('1')).toBeInTheDocument();
    expect(metric('Critical demand zones').getByText('6')).toBeInTheDocument();
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
      now={now}
    />);
    expect(metric('Reporting EVs').getByLabelText('Reporting EVs: unavailable')).toHaveTextContent('—');
    expect(metric('Available EVs').getByLabelText('Available EVs: unavailable')).toHaveTextContent('—');
    expect(metric('Critical demand zones').getByLabelText('Critical demand zones: unavailable')).toHaveTextContent('—');
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
      now={now}
    />);

    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Available EVs').getByText('1')).toBeInTheDocument();
    expect(metric('Open dispatches').getByText('1')).toBeInTheDocument();
    expect(metric('Critical zones').getByText('1')).toBeInTheDocument();
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
      now={now}
    />);
    expect(metric('Reporting EVs').getByText('1')).toBeInTheDocument();

    view.rerender(<FleetPulse
      fleet={{ data: updatedFleet, updatedAt: fresh }}
      heat={{ data: makeHeat(), updatedAt: fresh }}
      dispatches={{ data: { dispatches: [] }, updatedAt: fresh }}
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
      rentalDemandLevel={zone => demandLevel(zone, { moderate: 5, critical: 10 })}
      now={now}
    />);
    for (const label of ['Reporting EVs', 'Available EVs', 'Open dispatches', 'Critical demand zones']) {
      expect(metric(label).getByText('0')).toBeInTheDocument();
    }
  });
});
