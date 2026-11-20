# Local-first testing

Windows commands come first; for macOS see [macOS and Linux](#macos-and-linux).

Install Python 3.12+, Node.js 22+, and PowerShell. Install Python testing dependencies once:

    py -3 -m pip install -e ".[test]"

Then run this from VS Code PowerShell:

    .\scripts\test-local.ps1

The runner runs the full local Python pytest suite, dashboard Vitest tests and dashboard TypeScript build, and simulation console TypeScript build. It automatically runs npm ci when a frontend node_modules directory is absent. It exits 0 on success, nonzero on the first failure, with console [PASS]/[FAIL] only; no XML or HTML report file.

Pytest clears inherited deployment settings before it collects tests. Its subprocess fixtures use isolated loopback endpoints and ephemeral service state. The PostgreSQL telemetry check requires an explicit `--telemetry-postgres` flag and `TELEMETRY_TEST_DATABASE_URL` for a disposable `twin_history_test` database on a numeric loopback address and port. The infrastructure workflow provisions this database for the telemetry job.

## Full infrastructure verification

For data persistence, telemetry, containers, Kubernetes, or deployment changes:

    .\scripts\test-local.ps1 -Integration

This additionally uses Docker Desktop (Linux containers) and act (https://github.com/nektos/act; example installation: winget install nektos.act) to execute three existing CI jobs LOCALLY: disposable PostgreSQL telemetry tests, local Docker Compose smoke test, and Kubernetes manifest checks. act may download a large Linux runner image on first use. No real vehicle commands, external infrastructure, or production database are required.

If Docker/act is unavailable, default checks still run; the additional integration suite is UNVERIFIED. Run infrastructure-sensitive changes through full local integration before merging. The script is designed for Windows PowerShell; on Linux/macOS run the [equivalent commands](#macos-and-linux) individually.

If PowerShell restricts scripts:

    powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1

## macOS and Linux

The PowerShell runner uses Windows-only executable paths. From the repository root, after creating `.venv` (see [setup on macOS](setup-macos.md#5-local-development-environment)) and running `npm ci` in each frontend directory once:

    env -u TELEMETRY_TEST_DATABASE_URL .venv/bin/python -m pytest -q
    npm --prefix dashboard test -- --run
    npm --prefix dashboard run build
    npm --prefix simulation-console/frontend run build

Keep `TELEMETRY_TEST_DATABASE_URL` unset for the default backend tests; the Windows runner does this automatically. On Node.js 25 or newer, run the dashboard tests with `NODE_OPTIONS=--no-experimental-webstorage`; Node's built-in `localStorage` otherwise replaces jsdom's and the test setup fails.

For infrastructure or database changes, use `act` to run the `telemetry-postgres` and `compose-smoke` jobs in `.github/workflows/infrastructure-checks.yml` and the `manifests` job in `.github/workflows/kubernetes-portability.yml` locally. If Docker or `act` is unavailable, report the integration suite as UNVERIFIED.

## GitHub workflow fallback

GitHub Actions are manual-only (`workflow_dispatch`): open GitHub > Actions, pick the workflow and select Run workflow. Hosted runs consume billable minutes.

## Offline route fixture

Dispatch approves a relocation only with a verified `ONEMAP` route, or with a
labelled straight-line `OFFLINE_DEMO` route when it runs with
`ROUTING_PROVIDER=OFFLINE_DEMO` (the default without credentials, so the
deployed demo works offline). The moving EV rejects commands without a route.
So that tests can exercise the real
Dispatch, Fleet and movement state machines without OneMap credentials, the
test harnesses inject a deterministic test-only `RoadRoute(source="ONEMAP")`
from `tests/routing_fixture.py`. Its straight-line geometry is synthetic.
`tests/unit/test_routing_contract_regression.py` checks that production code
still refuses missing routes.


## Vitest worker startup timeout on Windows

The dashboard unit tests use Vitest with `jsdom`. On some machines,
especially when Docker Desktop/Kubernetes is using significant CPU,
the default parallel `forks` pool can fail **before any test executes**:

```text
[vitest-pool-runner]: Timeout waiting for worker to respond
Test Files  no tests
```

The dashboard Vitest configuration now uses one isolated `threads`
worker and runs test files sequentially (`fileParallelism: false`).
This changes **only how tests are scheduled**, not production code or
assertions. Vitest still gives each file its own isolated environment.

Verify the dashboard separately first:

```powershell
npm.cmd --prefix dashboard test -- --run
npm.cmd --prefix dashboard run build
```

If worker startup still times out, retry while the local Kubernetes/EV
simulation is idle, and record `node --version` and whether running
one test file succeeds. Increasing `testTimeout` does not fix a
**worker-start** timeout; do not silently suppress the errors. Once the
dashboard tests pass, run the full `scripts/test-local.ps1` workflow.
