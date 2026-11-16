# Parking, charging and ground-staff handoff

The Facilities service (port 8005) owns the parking and charger catalogue and
the simulated parking holds. Fleet owns charging tasks. Rebalancing produces
read-only charger advice. Nothing here operates a real car park or charger.

## Facility catalogue

Facilities come from one of two sources, shown on every snapshot as
`data_source` and on every facility as `source`:

- **MOCK**: 15 fictional car parks near the six zone centres. Each charging bay
  is counted inside parking capacity, and available charging bays inside
  available parking.
- **LTA**: real car park and charger observations from
  [LTA DataMall](lta-datamall.md), used as destinations for simulated EVs.

`GET /api/v1/facilities` (optionally `?zone_id=cbd`) and
`GET /api/v1/facilities/{facility_id}` return the catalogue with remaining
capacity. Available-space observations are planning inputs only.

## Parking holds

[CRDS](crds.md) reads facility freshness, parking and charger capacity before it
asks for road evidence. At approval, Dispatch rechecks the chosen facility and
takes a simulated hold before Fleet claims the EV. The hold lifecycle is:

1. **Held**: created by Dispatch for one dispatch ID.
2. **Released**: if the Fleet claim is definitively rejected. An ambiguous outcome keeps the hold until Dispatch reconciles it.
3. **Occupied**: after verified arrival. The bay stays occupied until the car leaves.

A rental start calls `POST /api/v1/vehicles/{vehicle_id}/parking/vacate` to
free the bay. Facilities refuses that while a charging cable is still connected.
The hold endpoints (`/facilities/{id}/holds`, `/holds/{dispatch_id}` and its
`complete` and `release` actions) are internal and blocked for the browser.

## Low-battery charger advice

`GET /api/v1/charging-advice` on Rebalancing suggests a charger for any EV
reporting SOC at or below **25%**. It is advice only. It creates no task, claim
or hold. The dashboard separately labels SOC at or below 30% as LOW and below 20%
as CRITICAL.

An EV qualifies when it is online with fresh telemetry, available, idle, and not
rented, claimed or already tasked. Stale EVs can appear as ineligible but never
as safe to send. A destination qualifies when its observation is fresh and open,
with at least one free charging bay and free parking.

Route distance is straight-line distance × the route factor, and predicted
arrival SOC is:

    current SOC − (distance × kWh/km × energy margin ÷ battery kWh) × 100

Candidates that would arrive below the operational reserve (20% by default) are
dropped, so a nearly flat EV may get no suggestion at all. Among the rest, the
shortest distance wins, then the highest arrival SOC, then the facility ID. A
dependency failure returns an error instead of an empty list.

In the dashboard, **Prepare charger route** fills the relocation form with the
EV, zone, purpose `CHARGE` and the advised facility ID. The operator then
requests a recommendation (which re-evaluates everything) and approves it as
usual. `facility_id` on `POST /api/v1/batch-recommendations` is only accepted
for `purpose=CHARGE`, one vehicle and quantity 1.

## Human cable handoff

Charging after an approved `CHARGE` relocation needs people in the loop. The
system monitors, recommends and moves the simulated car. A dispatcher approves
the move, and ground staff connect and disconnect the cable.

1. After arrival and claim release, the EV shows **Awaiting cable connection**.
   It is unavailable and does not gain charge.
2. A named team member selects **Confirm cable connected**.
3. The simulator charges the EV at the allocated charger's power, on the usual
   simulation time scale, up to 80%.
4. At the target it shows **Awaiting cable disconnection** and stays unavailable.
5. After **Confirm cable disconnected**, the EV shows **Ready for service** only
   once fresh telemetry confirms it is available.

Each `POST /api/v1/vehicles/{vehicle_id}/charging-cable` carries the
allocation's `dispatch_id`, a UUID `request_id`, `CONNECT` or `DISCONNECT`, and
`staff_name`. Retries return the original receipt. Cable state, the staff name
and timestamps are saved with the occupied Facilities allocation.

The checks fail closed:

- Connecting needs fresh, stopped, idle telemetry that is awaiting connection.
- Disconnecting needs fresh telemetry showing charging complete at the target.
- Active claims, rentals, tasks, a pending destination or a changed allocation block both actions.
- The simulator cannot gain charge without a connected allocation, and Fleet
  checks the cable is disconnected before accepting the EV as available again.

The typed name is attribution, not authentication.

## Charging tasks

Separately from charger relocations, an operator can queue a manual charging
task for a low-battery EV. A task reserves the EV against rentals and
relocation, but does not move or charge it.

    PENDING -> ASSIGNED -> IN_PROGRESS -> COMPLETED

Pending or assigned tasks can be cancelled. Assigning staff is only an operator
record. Completion fails unless fresh telemetry reports
`charging_state=COMPLETE` and SOC is at least 50%. Fleet rejects tasks for EVs
that are rented, claimed, stale, busy or already charged enough. Task requests
are version-checked and idempotent, and the queue is saved in Fleet's snapshot.

Do not create a manual task for an EV already in the cable handoff flow.
