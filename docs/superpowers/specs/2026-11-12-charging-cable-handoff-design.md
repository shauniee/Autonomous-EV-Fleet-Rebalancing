# Human charging cable handoff

Approved in chat: retain human dispatch approval; after simulated arrival require a ground-team cable connection before charging, and disconnection before vehicle availability returns.

## Intent and boundaries
Show supervised EV rental automation honestly: software monitors, recommends and executes approved simulated journeys; people connect and disconnect cables. Preserve the existing dashboard identity, manual task log, approval/recovery checks and default 80% target. No physical charger commands, robot claims, deployment, database migration, Git commit or remote actions.

## Approaches
The accepted approach is a backend-enforced connection/charging/disconnection workflow. A copy-only explanation cannot enforce the approved demonstration. Extending the independent manual task queue would block the existing moving simulator, so cable evidence belongs to the occupied Facilities allocation instead (implementation choice).

## Behaviour
- Occupied allocated charging bay: awaiting connection, unavailable, no SOC gain.
- Named ground team confirms connection for that specific allocation; simulator publishes a verified unchanged-SOC baseline, then charges to its configured target.
- At target: complete, awaiting cable disconnection, still unavailable.
- Named ground team confirms disconnection using fresh matching stopped/complete Fleet telemetry; only subsequent simulator telemetry makes the vehicle available.
- Missing/old evidence, pending relocation, wrong session, active rental/claim/task or invalid action fails closed. Identical request retries have one durable receipt. Confirmations persist with existing Facilities snapshots; no schema migration.
- Dashboard shows the live stage, charger, SOC, next action, attribution and recoverable errors using existing light/dark tokens, keyboard controls and small-screen layouts.
- Existing allocations missing cable evidence default to awaiting connection; no fabricated automatic confirmation.

## Verification
Tests cover physical simulation, API transitions/retries/failures/persistence, Fleet charging evidence/recovery/eligibility and dashboard actions. Run the full local suites and builds; use isolated browser fixtures where possible. Live Kubernetes and physical charging are separate and are not changed or claimed verified.
