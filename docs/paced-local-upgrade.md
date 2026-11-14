# Completed local paced-bulk upgrade

Status: **applied locally with explicit user approval**. All four deployments are
ready. All seven authorized journeys resumed in two waves and completed with
confirmed claim release. No changes have been committed or pushed.

## Images and order

Updated these deployments in docker-desktop / ev-fleet:

1. Rebalancing: `csc3104/ev-fleet-backend:bulk-local-1794588902909`.
2. Fleet: the same backend image; atomic legacy storage promotion before readiness.
3. Dispatch: the same backend image; atomic promotion and restored durable journal.
4. Dashboard: `csc3104/ev-fleet-dashboard:bulk-local-1794588902909`.

The three backend deployments retain one replica and Recreate strategy. Dashboard
retains its existing RollingUpdate strategy. Server dry-runs passed for all four.
Simulator controllers, replicas, command arguments and ownership IDs are unchanged.
Do not use the full simulator-restarting startup script for this update.

Pause new approval submissions for the short rollout. After each backend is ready,
verify image identity and compare retained claim/command IDs, positions and SOC
against the pre-update inventory. Do not automatically roll an old image back
after a namespace has promoted. No new test relocations are proposed in this stack.

## Backup

A consistent read-only PostgreSQL backup was created before the proposal:
`C:\Users\User\.codex\visualizations\2026\11\12\01a1cd80-b77f-7881-afc7-912dede0db70\approval-latency\before-paced-upgrade.dump` (20,722,608 bytes).
SHA-256: `220cd3189f9628c6f53d33ed774e6ce6fce6f264e155361f47f9bd82d10707a1`.
The pg_restore catalog check passed. Fleet/Dispatch are still in legacy format;
role permissions for the new table and snapshot writes passed read-only inspection.

## Existing paused journeys

All seven retain their prior claims and validated checkpoints. Read-only recovery
and safe-end assessments found no blockers. They predate the new four-slot policy
and prevent fresh admissions until existing work is resolved.

| EV | Saved battery | Can resume | Can safely end |
|---|---:|---|---|
| ev-moving-extra-15 | 76.9% | Yes | Yes |
| ev-moving-extra-3 | 85.9% | Yes | Yes |
| ev-moving-extra-9 | 66.0% | Yes | Yes |
| ev-moving-01 | 88.1% | Yes | Yes |
| ev-moving-extra-2 | 77.2% | Yes | Yes |
| ev-moving-extra-10 | 70.1% | Yes | Yes |
| ev-moving-extra-12 | 87.0% | Yes | Yes |

Recommended operator decision: resume these already-approved journeys in waves
of at most four, waiting for arrival and confirmed claim release before the next
wave. Re-assess immediately before each recovery request; preserve physical
position, battery, command identity and charging controls. Resuming or ending is
a separate human decision. The user explicitly approved resuming all seven in
waves. The first four completed before the remaining three were resumed. All
seven are now SUCCEEDED with RECOVERED status and released claims.

## Rollback boundary

Before any paced job is created, a separately authorized maintenance procedure
can quiesce writers and export the latest normalized state back to the legacy
snapshot format. Never rewind a pre-upgrade backup after journeys have progressed.
Once paced jobs exist, older images also lack the job protocol: finish or explicitly
cancel pending authorizations and reconcile uncertain effects first. A downgrade
then requires a reviewed conversion that preserves authorization audit as well as
physical state; this change does not provide a generic automatic conversion for
new jobs. Keep current state and use a compatible corrected image instead of an
image-only rollback.

## Checks

The isolated paced50 run completed all 50 after one approval (max 4 active,1.006s
acceptance,231.46s at30x simulated travel). Two transient retries recovered
without manual retry or RECOVERY_WAIT. The29 PostgreSQL storage/queue checks pass.
Windows full checks passed: 613 backend tests (30 opt-in DB skips), 86 dashboard
tests and both frontend builds. Separately, all 29 PostgreSQL checks passed; the
paced50 completion test also passed. The production image dependency/startup and
Nginx asset smoke checks passed. The prior full run's two synchronous HTTP
assertions now verify rejection after admission; all 43 affected tests passed. Browser visual inspection and Python
Pyright/Pylance analysis remain unavailable in this environment.


## Final local verification

Verified on 14 November 2026 (Asia/Singapore): all four updated deployments are
ready; Fleet/Dispatch are in normalized storage with 20 vehicles and 31 dispatch
records. The persistent Fleet identity and all seven original command IDs were
retained. The upgrade itself preserved positions, distances and battery values
before operator-authorized recovery began. Journeys then progressed normally and
completed without further operator intervention. No new test dispatches were sent
to the existing stack.

The dashboards are reachable at http://127.0.0.1:8080 and
http://127.0.0.1:8090. All 20 EVs are ONLINE with RUNNING supervision;15 are available
and five retain existing charging/stopped-workflow restrictions. There are no
active relocation claims. A live read-only OneMap estimate succeeded for 25.688km.
The task artifacts retain pre/post state, recovery request journals, image IDs,
backups and final verification output. A completed-state backup follows the
pre-upgrade backup; neither is an automatic legacy-image rollback.


Post-completion backup: `C:\Users\User\.codex\visualizations\2026\11\12\01a1cd80-b77f-7881-afc7-912dede0db70\approval-latency\after-paced-upgrade-completed.dump` (21,490,570 bytes),
SHA-256 `94c34add61d9eb3280b578d407203ec7637da58281b93a75923bc7021b164e0f`. Read-only PostgreSQL inspection confirmed all seven
SUCCEEDED states and released claims in normalized rows.
