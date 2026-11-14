# Low-SOC charger advice (simulation only)

This addition **does not autonomously dispatch EVs**. It provides a continuously
refreshed read-only recommendation for an operator to review.

## Trigger and safety rules

- The Operator Dashboard already raises **LOW at SOC ≤30%** and **CRITICAL below 20%**.
- The Rebalancing API `GET /api/v1/charging-advice` suggests destinations when an
  EV reports **SOC ≤25%**. This is a configurable-in-code advisory trigger, not a
  separate charging task or command. EVs with stale telemetry can appear in the
  advisory list as ineligible, never as safe to dispatch.
- Each candidate must have fresh open facility observations, at least one
  **simulated available charging bay**, and available parking space.
- An EV must be online with fresh accepted telemetry, available, idle, and not
  rented, claimed or already assigned a charging task.
- Route distance = haversine straight-line distance × configured route factor.
  Predicted arrival SOC = current SOC − (distance × kWh/km × configured
  energy margin / battery kWh) × 100.
- A candidate is rejected if **predicted arrival SOC is below the configured
  operational reserve (20% by default)**. A critically depleted EV may have
  no safe charger to drive to; no unsafe dispatch is suggested.
- From eligible destinations, choose the shortest estimated simulated distance,
  then highest remaining arrival SOC, then stable facility ID.
- Dependency or parsing failures return a service error rather than an
  incorrectly empty advisory. No claims or charger holds are made.

These are conservative *simulation estimates*, not real navigation, confirmed
charger availability, connector compatibility, guaranteed access, or proof
that a charger is working. When data is stale, no new route should be approved.

## Operator workflow

1. Open **Battery alerts & ground-staff charging log** on the Operator Dashboard.
2. Under **Suggested charging destinations**, choose **Prepare charger route**.
   This preselects the EV, zone, purpose `CHARGE`, and **the exact advised
   facility ID** in the existing relocation request form.
3. Click **Recommend EV**. The backend re-evaluates current eligibility,
   charging capacity and energy reserve; it may return no assignment if conditions
   changed. The optional `facility_id` on
   `POST /api/v1/batch-recommendations` is only allowed for
   `purpose=CHARGE`, one selected vehicle and quantity 1.
4. Review the fresh matching assignment, then separately click to approve the
   dispatch. The existing Dispatch Service revalidates and reserves a simulated
   destination before issuing the EV command.

Manual ground-staff charging tasks remain a separate workflow. A charging task
reserves the EV against new relocation but does not automatically move it.
After an approved CHARGE relocation, the EV waits for a named ground team to
confirm **Cable connected** in Charging operations. Simulated charging runs to
80%; the EV remains unavailable until staff confirm **Cable disconnected** and
the simulator publishes fresh availability. See [Human charging handoff](charging-cable-handoff.md).
The dashboard polls for advice while open. There are no push/mobile alerts,
no unattended dispatcher and no new persistence database.
