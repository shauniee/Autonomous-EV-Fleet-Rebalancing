# CSC3104 Research Article, Demo and Final Submission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the verified EV Fleet implementation and reproducible evaluation into a correctly formatted research article, independent testing instructions, recorded demo, and complete submission before 15 November 2026.

**Architecture:** Freeze a known-good source revision and build a linked evidence index. Derive paper figures/tables from the validated raw study and deployment observations, and script the video and test instructions against that same revision. Keep all synthetic assumptions and blocked tests visible.

**Tech Stack:** Existing GitHub repo, Docker Desktop + Kubernetes, PowerShell, Python experiment scripts, CSV/SVG outputs, IEEE or ACM proceedings template, PDF export, screen recording.

**Spec:** `docs/superpowers/specs/2026-11-04-csc3104-delivery-design.md`

## Global Constraints

- Course deadline: **23:59 Sunday 15 November 2026**.
- Article: around **3,000 words, at most 6 pages**, IEEE/ACM proceedings format; abstract **at most 200 words**.
- Submit **four deliverables**: source code/image or repository URL, `GROUPNN.pdf`, `GROUPNN_INSTRUCTIONS.pdf`, and video/video URL.
- Video length **10–15 minutes**.
- The lecturer says sophisticated GUI is not required; distributed/cloud-native design, correctness, failures, concurrency, originality, evaluation and reporting matter more.
- Unknown group number `NN` and group member assignments must be supplied by the group before final file naming; placeholders must not reach upload.
- Never invent benchmark improvements, live LTA data, actual AV autonomy, production-scale capacity or externally verified traffic accuracy.

## Review Focus

1. **Report shows a figure but its run SHA differs from the released app:** label clearly as historical or rerun on the frozen source (Task 1).
2. **A policy loses on some scenarios:** report it truthfully; the contribution is an evaluated engineering trade-off, not guaranteed superiority (Task 2).
3. **Marker lacks OneMap/LTA keys:** safe mock/synthetic demonstration still starts and exercises human approval (Task 3).
4. **Recording shows stale/unavailable EV at the wrong moment:** explain the real status or restart from a safe clean demo; never edit metrics to hide it (Task 4).
5. **Final PDFs exceed length/page limits or lack references:** package must fail audit until corrected (Task 5).

---

### Task 1: Create the final release evidence index

**Files:**
- Create: `docs/evidence/2026-11-04/submission-evidence-index.md`
- Review: the G0–G4 plans, raw trial manifests, CI statuses and Kubernetes checks.

**Interfaces:**
- Consumes: exact frozen Git SHA, immutable experiment run ID, figure filenames, logs and status matrix.
- Produces: one source-of-truth map used by report, tests and video.

- [ ] **Step 1: Freeze a candidate release commit.** Record `git status --short`, `git rev-parse HEAD`, GitHub merged PR identifiers and full test/build result states.
- [ ] **Step 2: Map each important claim to evidence.** Example: single vehicle claim -> concurrent approval test and safe demonstration; paired energy-aware advantage -> `paired-effects.csv`; K8s resilience -> exact Pod log and fault trace.
- [ ] **Step 3: Document measured versus derived data.** State which results use simulated clock, estimated route factor, sampled LTA, GET-only HPA loads, or current host wall-clock measurements.
- [ ] **Step 4: Record limitations and unresolved tests.** Windows Vitest worker issue if still blocked, unvalidated live external endpoints, untested large-load conditions, non-production single-node Kubernetes.
- [ ] **Step 5: Commit index.** `git add docs/evidence/2026-11-04/submission-evidence-index.md && git commit -m "docs: link release claims to reproducible evidence"`

**Acceptance:** no report/video claim lacks a committed code revision and an evidence origin.

### Task 2: Write the six-page article and related-work argument

**Files:**
- Create: `submission/report-source/` (IEEE/ACM Word or LaTeX source decided by team)
- Produce: `submission/GROUPNN.pdf` from source after review
- Use: `docs/strategy-evaluation.md`, `docs/architecture.md`, G4 evidence and diagrams.

**Interfaces:**
- Consumes: evidence index and chosen main contribution claim.
- Produces: an approximately 3,000-word, <=6-page conference-formatted research article.

- [ ] **Step 1: Agree on a defensible contribution sentence.** Suggested: an explainable, human-approved distributed EV rebalancing simulation combining versioned telemetry, atomic vehicle/parking claims, failure reconciliation, realistic routing constraints, and a paired evaluation against a nearest-EV baseline. This is a *proposed framing*, not proof that no prior system does the same.
- [ ] **Step 2: Research related work.** Retrieve and read primary sources about energy-aware fleet rebalancing, demand-driven vehicle allocation, Kubernetes/distributed service reliability, and fault-tolerant dispatch. Record precise bibliographic metadata and cite only papers actually read. Compare objectives, constraints, coordination, experiments, and limitations; do not invent a novelty claim.
- [ ] **Step 3: Draft sections in the course-required order.** Abstract (<=200 words), Introduction/motivation, System Design (architecture and sequence diagram), Implementation (ownership, concurrency/optimistic locking/idempotency, failures), Evaluation (quantitative charts/tables), Related Work, Conclusions/future work, References.
- [ ] **Step 4: Insert reproducible figures.** At minimum: deployment architecture, human-approved relocation sequence/failure branch, matched policy results with denominators/uncertainty, and one deployment performance figure if reliable. Caption synthetic vs real measurements explicitly.
- [ ] **Step 5: Verify technical claims with another group member.** Confirm claims such as exactly-once vs at-least-once, OneMap/OSRM available in demonstration, 20 *actual* simulated Pods vs 100 EV-equivalent GET HPA workload, and genuine failure handling.
- [ ] **Step 6: Export PDF and audit.** Count pages (<=6) and approximate words (~3000), check equations/graphs, references, DOI/URLs, figure readability, abstract, and accessible text. Do not let images/table spill past margins.
- [ ] **Step 7: Commit the editable source and final `GROUPNN.pdf`** once the group number is confirmed.

**Acceptance:** article meets course format, has evidence for every quantitative claim, gives a realistic originality argument and names limitations.

### Task 3: Produce independent testing instructions for a marker

**Files:**
- Use: `README.md`, `docs/kubernetes-local.md`, `scripts/setup-local.ps1`, `scripts/start-kubernetes.ps1`, `scripts/test-local.ps1`
- Create editable instructions source, then `submission/GROUPNN_INSTRUCTIONS.pdf`.

**Interfaces:**
- Consumes: frozen source and approved safe demonstration path.
- Produces: a stand-alone guide that a grader can follow without destructive scripts or undisclosed private credentials.

- [ ] **Step 1: Write prerequisites exactly.** Docker Desktop Linux containers + Kubernetes, Windows PowerShell, Python/.venv, npm/Node for frontend checks, kubectl `docker-desktop` context, ports 8080/8090. Specify the actual image build/startup command and optional offline/mock configuration.
- [ ] **Step 2: Give safe startup steps.** From a clean checkout run `powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1`; wait for readiness, open the operator dashboard at 127.0.0.1:8080 and simulation console at 127.0.0.1:8090. Explain `-ConnectOnly` for an existing cluster. **Separate** credential-free startup/recommendations from routed vehicle movement: production Dispatch needs an authoritative OneMap route/token, and `ROUTING_PROVIDER=SIMULATED` correctly returns `ONEMAP_ROUTE_REQUIRED` instead of inventing roads.
- [ ] **Step 3: Give exact reproducible test actions.** Show initial Fleet status and zone demand, change simulated demand to create a hotspot, request a recommendation, and confirm no movement occurs before approval. **With verified OneMap routing**, explicitly approve and watch road movement, arrival and claim release. **Without OneMap**, show the expected fail-closed refusal and a separately labelled offline route-fixture integration test for acknowledgement and arrival, never a production straight-line journey.
- [ ] **Step 4: Describe test commands and expected results.** `python -m pytest -q`, `npm.cmd --prefix dashboard run build`, `npm.cmd --prefix dashboard test -- --run`, `docker compose config`, `kubectl kustomize .\k8s`; explain which checks are optional, blocked or need external credentials.
- [ ] **Step 5: Add troubleshooting and restoration.** Missing Docker daemon, Kubernetes context, port-forward, inactive or stale telemetry, no road route, unavailable traffic data; never instruct users to erase persistent volumes to repair a demo.
- [ ] **Step 6: Perform a cold-read walkthrough.** A second group member starts with instructions and notes ambiguities; update accordingly.
- [ ] **Step 7: Export PDF and verify** the exact `GROUPNN_INSTRUCTIONS.pdf` filename, copy/pasteable commands, links, screenshots and page readability.

**Acceptance:** a grader unfamiliar with the implementation can run a safe complete path and understand genuine environmental limitations.

### Task 4: Record and validate the 10–15 minute demonstration

**Files:**
- Create: `docs/evidence/2026-11-04/video-script.md`
- Produce: `submission/<video-file-or-link>`.

**Interfaces:**
- Consumes: release commit, paper architecture/evaluation figures and safe testing instructions.
- Produces: a concise team demonstration with authentic running-state evidence.

- [ ] **Step 1: Storyboard approximately 12 minutes.** 0–1 motivation/research question; 1–3 architecture and service ownership; 3–6 live demo (demand → recommendation → review → explicit approval → movement); 6–8 concurrency/failure behaviour; 8–10 matched experiments, plots and measured limitations; 10–12 conclusion and member contributions.
- [ ] **Step 2: Preflight the local cluster.** Confirm no in-flight rentals, dispatch claims or charging before restart. Set reproducible demand and verify traffic feed mode. Confirm OneMap is configured, token valid, and the route service reachable before promising a live movement recording; otherwise prepare the clearly labelled test-harness alternative.
- [ ] **Step 3: Record the real interface and logs.** No fabricated movement, edited test outputs or speculative performance overlays. Show normal movement **only** if verified with authoritative road routing; otherwise show recommendation/approval refusal in the live UI plus a clearly labelled offline integration trace. Include a bounded fault/uncertainty demonstration when available.
- [ ] **Step 4: Record team participation** so each member can explain their technical contribution, and keep identities/roles aligned with the report and peer evaluation.
- [ ] **Step 5: Review the final recording.** Duration 10–15 minutes, clear audio, legible numbers on screen, no secrets/API tokens shown, links accessible, video actually plays end-to-end.
- [ ] **Step 6: Add the video URL/path to the submission index** and commit the script.

**Acceptance:** viewer sees how cloud-native parts interact and understands what is simulated, what is measured, and why the decision policy matters.

### Task 5: Final package and submission freeze

**Files:**
- Final: repo/image or GitHub URL, `submission/GROUPNN.pdf`, `submission/GROUPNN_INSTRUCTIONS.pdf`, video file/link
- Review: `docs/evidence/2026-11-04/submission-evidence-index.md`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: complete, unambiguous upload package with a traceable release tag.

- [ ] **Step 1: Verify four required items** against the course brief, correct `NN` and file names, no broken links; confirm repository access permissions for marker.
- [ ] **Step 2: Check final Git revision and reproducibility.** Source tag/commit matches report experiments, Docker image build and video demonstration. Do not include local secrets or `.env` tokens.
- [ ] **Step 3: Check the article page count, video duration, report figures, test instructions and optional peer-evaluation procedure.**
- [ ] **Step 4: Assign one group member to upload all four items and another to independently verify their presence and readability in the LMS.** Complete before 23:59 Sunday 15 November 2026; do not leave all checks to the final minute.
- [ ] **Step 5: Record a submission checklist with actual completion times** and link it from the final README/release notes without copying private LMS details.

**Acceptance:** the deliverables are complete, readable and reproducible, with no unverified claims hidden as passing.

## Deadline stop rule

From 12 November onward, no new feature branch or research-method change may displace required report/instructions/video work unless a defect prevents the demonstrable safety-critical flow. If live routing or a secondary feature fails, switch to the existing truthful mock/simulation path and state its limitation in the article. Do not trade a complete submission for speculative polish.
