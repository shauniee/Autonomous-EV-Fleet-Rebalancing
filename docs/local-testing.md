# Local-first testing (Windows)

Install Python 3.12+, Node.js 22+, and PowerShell. Install Python testing dependencies once:

    py -3 -m pip install -e ".[test]"

Then run this from VS Code PowerShell:

    .\scripts\test-local.ps1

The runner runs the full local Python pytest suite, dashboard Vitest tests and dashboard TypeScript build, and simulation console TypeScript build. It automatically runs npm ci when a frontend node_modules directory is absent. It exits 0 on success, nonzero on the first failure, with console [PASS]/[FAIL] only; no XML or HTML report file.

The script temporarily removes TELEMETRY_TEST_DATABASE_URL so default backend tests cannot use an existing PostgreSQL connection. The dedicated PostgreSQL test will be skipped by pytest unless a disposable database is explicitly provisioned.

## Full infrastructure verification

For data persistence, telemetry, containers, Kubernetes, or deployment changes:

    .\scripts\test-local.ps1 -Integration

This additionally uses Docker Desktop (Linux containers) and act (https://github.com/nektos/act; example installation: winget install nektos.act) to execute three existing CI jobs LOCALLY: disposable PostgreSQL telemetry tests, local Docker Compose smoke test, and Kubernetes manifest checks. act may download a large Linux runner image on first use. No real vehicle commands, external infrastructure, or production database are required.

If Docker/act is unavailable, default checks still run; the additional integration suite is UNVERIFIED. Run infrastructure-sensitive changes through full local integration before merging. The script is designed for Windows PowerShell; using Linux/macOS requires running the equivalent commands individually.

If PowerShell restricts scripts:

    powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1

## GitHub workflow fallback

GitHub Actions are manual-only (workflow_dispatch). After the PR is merged, GitHub > Actions > desired workflow > Run workflow will run an on-demand GitHub-hosted verification and consume billable minutes. Review required branch status checks before merge, as manual-only workflows no longer trigger on PRs.
