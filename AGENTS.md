# Local-first workflow for Codex and coding assistants

After code or test changes run .\scripts\test-local.ps1 in Windows PowerShell, and resolve failures before reporting readiness.
Use powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1 if local execution policy blocks scripts.
For telemetry, Docker Compose, Kubernetes manifests, SQL/database or deployment-sensitive changes also run .\scripts\test-local.ps1 -Integration with Docker Desktop and act. Never target existing production/Kubernetes/PostgreSQL data.
The default script clears TELEMETRY_TEST_DATABASE_URL while executing local Python tests. Never override this with production credentials.
If integration tools aren't installed, mark the integration suite NOT VERIFIED, not passed.
No separate test report files. Only console results and a nonzero exit code on failure.
Existing GitHub Actions workflows are manual fallback only (workflow_dispatch). Do not restore automatic triggers without explicit approval.
