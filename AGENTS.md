# Local-first workflow for Codex and coding assistants

## Working rules

- Before editing, inspect the relevant code and `git status --short`. Preserve existing uncommitted work.
- Implement only the accepted task or phase. Keep changes small and follow existing patterns.
- Format new and modified code consistently with the project's existing style and formatter configuration. Avoid unrelated formatting changes.
- For Python, follow PEP 8 and write type-correct code that satisfies Pylance diagnostics using the project's interpreter and any existing analysis settings. Fix diagnostics introduced by your changes; do not weaken checks or add blanket suppressions to hide them. If Pylance or Pyright is unavailable, report that verification limitation.
- Do not stage, commit, push, interact with GitHub, deploy, or change external services without explicit authorization for that action. Do not run migrations or change existing databases without explicit authorization.

## Local checks on Windows and macOS

Run commands from the repository root using the host's shell and existing project environment. On Windows, Python is normally `.venv\Scripts\python.exe` and npm is `npm.cmd`; on macOS, use `.venv/bin/python` and `npm`.

Choose checks relevant to the change; respect explicit user instructions about testing. Documentation-only changes need a content review and `git diff --check`. Run broader checks when changes affect multiple areas.

Windows full local checks:

```powershell
.\scripts\test-local.ps1
```

macOS equivalents (the PowerShell runner uses Windows-only executable paths):

```sh
env -u TELEMETRY_TEST_DATABASE_URL .venv/bin/python -m pytest -q
npm --prefix dashboard test -- --run
npm --prefix dashboard run build
npm --prefix simulation-console/frontend run build
```

Use `npm ci` in the relevant frontend only when dependencies need installing. Keep `TELEMETRY_TEST_DATABASE_URL` unset for default backend tests; the Windows runner handles this automatically.

## Integration and reporting

- For infrastructure or database behavior changes, run relevant disposable integration checks when Docker and `act` are available. Windows supports `.\scripts\test-local.ps1 -Integration`; macOS can use `act` to run the `telemetry-postgres` and `compose-smoke` jobs in `.github/workflows/infrastructure-checks.yml` and the `manifests` job in `.github/workflows/kubernetes-portability.yml` locally.
- Never use existing production, Kubernetes, or PostgreSQL data for tests. Keep integration resources isolated from existing stacks, including cleanup operations.
- If checks are blocked by missing tools, permissions, or environment issues, report them as `NOT VERIFIED` and finish the work that can be completed. Do not expand the task into unrelated tool installation or environment repair.
- Report checks run, failures, and limitations. Do not claim runtime verification from a build or static check. Keep results in the response unless a separate report is requested.
- GitHub Actions workflows remain manual-only (`workflow_dispatch`). Do not trigger remote workflows or change their triggers without explicit approval.

See `docs/local-testing.md` for optional troubleshooting; its PowerShell commands apply to Windows.
