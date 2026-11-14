# Durable paced bulk relocation

User decision: a 50-EV human-approved batch may start gradually in the background;
transient failures must be handled automatically rather than requiring manual retry.
Preserve operator-only dispatch and charging cable controls. Work locally; do not
commit, deploy or modify the existing database without authorization.

## Binding behavior

POST /api/v1/batches durably accepts a job and promptly returns status QUEUED.
It validates the fresh original batch and freezes the exact human-approved vehicle,
facility, target, policy and recommendation per child. It never substitutes an EV
or destination. Stable UUID5 child request IDs prevent duplicate remote effects.
Queue intent belongs in per-dispatch durable records, not large batch metadata.
Approval is accepted before the original recommendation expires. Its execution
window is bounded to 30 minutes, shown in the confirmation. Later admission uses
fresh route, Fleet, capacity, demand, traffic, battery and CRDS policy checks.
The original recommendation TTL still governs single/direct approval.

A single Dispatch background worker admits at most one item at a time, at least
one second apart, while at most four journeys/uncertain resource acquisitions are
active globally (including other batches and single dispatches). Count uncertainty
conservatively. Requested/unknown remote effects reconcile the SAME exact claim;
never clear or invent claim IDs after a lost response. Queue retries use bounded
exponential backoff. Dependency timeouts/busy/temporarily unhealthy runtime may
retry automatically, but genuine battery/route/ownership/capacity/policy rejection
is visible and terminal. An expired simulator lease requires operator recovery.
No queue may silently resume an expired owner or extend simulator leases.

Pending jobs, per-item attempts, next-attempt timestamps and authorization persist
before remote actions. Restart restores them without new human approval. Fresh
pending intents may replan current evidence only for the same approved assignment.
No new mutations begin after the job authorization expires. Existing uncertain
claims continue exact reconciliation even after authorization expiry.

BatchDispatch adds job_status QUEUED/RUNNING/COMPLETED/EXPIRED, accepted_at,
expires_at, admitted_count, completed_count, queued_count and in_flight_limit.
Keep existing approval-status field semantics for admitted claims; add status
QUEUED/PROCESSING for batch-level work awaiting admission. DispatchState adds
QUEUED. Per-item fields queue_status QUEUED/WAITING/ADMITTED/REJECTED/CANCELLED,
attempts and retry_at distinguish automatic waiting from a genuine rejection.
Existing close-unstarted action can cancel only proven unstarted items and
must retain claims that may have executed. Polling GET never advances work.

CRDS revalidation gains optional approved_batch_request_id. When present,
Rebalancing obtains the authoritative frozen intent from Dispatch GET
/api/v1/batches/{batch}/items/{recommendation}/intent. Verify request/dispatch/
recommendation identity and live authorization. Do not trust caller-supplied
recommendation content. Reuse existing exact-vehicle/facility plan_one pipeline,
model/policy checks and planning revision CAS; no waived benefit/safety checks.
For queued admission Dispatch.prepare reads its durable approved intent and uses
its bounded execution expiry; ordinary/direct preparation is unchanged.

Expose up to 50 requested EVs in contracts/dashboard. Physical eligibility and
parking/charging capacity still limit recommendations. Preserve legacy direct
service.approve_batch for existing internal callers/tests if useful, but the HTTP
batch endpoint must enqueue. Queued records must not be reconciled as ordinary
unapproved PENDING records before the job worker admits them.

## Verification

TDD: prompt enqueue before blocked dependencies; restart and same-ID replay;
backoff then success without duplicate holds/claims; global active cap including
unknown ownership; expired approval vs exact uncertain reconciliation; safe close;
battery/policy rejection; CRDS intent identity/policy checks; UI polling/progress.
Disposable PostgreSQL round trips prove queue durable fields. A real HTTP 50-EV
queued workload must finish all feasible relocations without manual retries or
RECOVERY_WAIT. It may use accelerated simulated travel but retains 2-second RPC
deadlines and 30-second leases. Record acceleration and resource limits. Do not
claim the earlier 50-simultaneous workload passed: it failed in both single-loop
and five-driver-process configurations.
