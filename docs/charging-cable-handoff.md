# Human charging handoff

The demonstration shows human-supervised automation: the system monitors the
fleet, recommends destinations and executes approved simulated movement. A
dispatcher authorizes relocation. Ground staff connect and disconnect cables.
Kubernetes operates the supporting services; it does not perform physical work.

## Demonstration

1. In Operator Dashboard, open **Charging** and review charger advice for an
   eligible low-battery EV. Prepare a charger route, request a recommendation,
   then review and approve the dispatch as before.
2. Follow the approved journey. Once arrival/parking and claim release are
   confirmed, **Ground-team charging handoff** shows **Awaiting cable connection**.
   The EV is unavailable and its battery does not gain charge.
3. Enter the ground team's name and select **Confirm cable connected**. This
   records the simulated act of inserting the cable for this charging visit.
4. Watch **Charging in progress** and fresh SOC readings. Energy is advanced by
   the EV simulator, using the allocated charger power and existing simulated
   time scale. The default target is 80%.
5. At the target the stage becomes **Awaiting cable disconnection**. The EV is
   still unavailable for rentals and relocation.
6. After the simulated physical unplugging, select **Confirm cable disconnected**.
   The service records the action; **Ready for service** appears only after
   fresh EV telemetry confirms availability. Completion alone cannot release it.

Do not create a manual assistance task for an EV already following this flow.
That separate queue reserves an EV and does not drive or charge it.

## Evidence and recovery

- Cable state, named-team attribution and timestamps are saved with the occupied
  Facilities allocation in the existing snapshot store. No table/schema migration.
- Each POST to `/api/v1/vehicles/{vehicle_id}/charging-cable` includes the
  allocation's `dispatch_id`, a UUID `request_id`, `CONNECT` or `DISCONNECT`, and
  `staff_name`. Retrying the same request returns its original receipt without
  repeating the transition, including after a restart or later disconnection.
- Connection requires matching fresh, stopped, idle awaiting-connection telemetry.
  Disconnection requires matching fresh complete telemetry at the charge target.
  Active claims, rentals/tasks, a pending destination or a changed allocation
  prevent confirmation. Missing evidence is unavailable rather than an empty queue.
- The simulator cannot gain charge without a verified connected allocation.
  Fleet also verifies the first transition back to NOT_CHARGING against a
  disconnected cable before accepting availability.
- A lost response retains the original request for retry. If polling confirms
  the committed cable stage, the interface replaces the retry error with a
  confirmation. Restore connectivity and refresh the bay/vehicle observations
  if the service cannot verify an action.
- An old saved charging visit without cable records is paused safely on guarded
  idle recovery as AWAITING_CONNECTION/UNAVAILABLE, preserving its position,
  SOC and odometer. It requires a fresh human confirmation and never credits
  charging downtime or fabricates a connected cable. Existing legacy idle/owner
  fencing and unresolved-journey checks still apply.

## Scope

This is a local simulation administration interface. A typed team name is
attribution, not staff authentication or proof of real cable insertion. There is
no robot, physical charger command, new unattended dispatch mode or claim of
production safety/security. Deploy compatible backend, simulator and dashboard
images together using the existing deployment procedure when workflows are idle.
Source edits do not update a running Kubernetes deployment automatically.
