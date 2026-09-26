# Phase 3B verification

Verified on 27 September 2026 (Singapore time).

| Check | Observed result |
|---|---|
| Existing Python backend suite | **108 passed**, unchanged tests; one existing Starlette/httpx deprecation warning |
| Frontend component and polling tests | **13 passed** |
| Playwright real-backend browser suite | **2 passed** |
| TypeScript check and Vite production build | Passed |
| npm dependency audit after Vitest patch update | **0 vulnerabilities reported** |
| `docker compose --profile relocation config --quiet` | Passed |
| `git diff --check` | Passed; Git emitted LF/CRLF conversion notices |
| Dashboard container build, Nginx runtime and Compose journey | **Not verified: Docker daemon unavailable** |

## Evidence

- [Backend JUnit report](evidence/phase3b-backend-tests.xml)
- [Frontend JUnit report](evidence/phase3b-frontend-tests.xml)
- [Browser JUnit report](evidence/phase3b-browser-tests.xml)
- Browser screenshots, traces and service logs are generated under `dashboard/test-results/` (ignored build/test output).

The browser journey uses four real FastAPI processes and two simulated EV nodes on isolated ports. It proves that recommendation alone creates no dispatch, explicit confirmation creates a dispatch, an intermediate position differs from both origin and destination, SOC decreases, execution is acknowledged, the dispatch succeeds, the Fleet claim is released, and the destination becomes `cbd` with availability restored. The second test verifies that tile and Demand failure leave Fleet and relocation panels available.

Component tests separately exercise backend severity independent of raw demand, all six zones, shortage labels, backend explanation and exclusions, no eligible EV, expiry, stale telemetry, target changes, late recommendation responses, explicit confirmation, UNKNOWN/timeout rendering, Fleet/Demand outages, partial/null values, stable approval IDs after a lost response and reload, retained last-known data, and non-overlapping/cancelled polling.

The ordinary browser suite stubs public map tiles. A separate run with `REAL_MAP=1` also passed, confirming actual successful OpenStreetMap tile responses. Desktop, narrow-screen and confirmation screenshots were visually inspected. A final responsive-map check verifies that all plotted markers remain within the mobile map bounds and the document has no horizontal overflow; the fleet table scrolls within its own panel.

No backend contracts, dispatch lifecycle, Phase 1/2 tests or Phase 3A business logic were modified for Phase 3B. Pre-existing uncommitted Phase 3A changes were retained.

## Runtime boundary

Local Node v25.3.0/npm 11.6.2, the repository Python virtual environment and Playwright Chromium were used. Windows sandbox restrictions required elevated tool execution for npm downloads, build tooling and browser subprocesses. These checks are local-process proof, not Docker runtime proof. Docker reported the engine named pipe was absent; Compose validation succeeded despite a separate restricted-access warning for the user Docker configuration file.

When Docker is available, run the explicit-service command in [the dashboard guide](phase3b-dashboard.md), check container health, and repeat the human-approved journey through port 8080. This remaining check must exercise the Nginx proxy and container network, not only the Vite development proxy.

The backend remains intentionally in memory. A pending approval in browser session storage does not make backend commands durable across service restarts.
