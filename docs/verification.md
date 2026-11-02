# Initial service foundation verification — 2026-09-26

Historical initial service foundation evidence is retained below. Docker became reachable during the
subsequent dispatch and relocation work; see [dispatch and relocation verification](dispatch-relocation-verification.md) for actual
image/container/network and relocation-recovery evidence that supersedes the
environment limitation recorded here.

Environment: Windows, Python 3.10.1, workspace-local `.venv`. Dependencies were
installed for this project only; `python -m pip check` reports no broken requirements.

## Executed checks

- `python -m pytest -q`: 37 passed. Includes a real three-process Uvicorn HTTP test
  with two independent stationary EV nodes, higher-SOC farther-EV selection,
  recommendation retrieval/expiry and stale-telemetry exclusion.
- Unit/API checks cover input constraints, Singapore zone seeds, concurrent vehicle
  and demand updates, telemetry ordering and session replacement, duplicate freshness,
  operational reserve boundaries, charging freshness, tie breaking, dependency
  timeout/unavailability/malformed data, error request IDs and health endpoints.
- `python -m pip check`: passed.
- `docker compose config --quiet`: passed. Docker emitted a local config access warning.
- `git diff --check`: passed; Git reported a Windows LF/CRLF conversion notice.

The first real HTTP run found a Python 3.10 RFC3339 `Z` parsing incompatibility.
The parser was corrected and a dedicated regression check added before the passing run.
The installed Starlette TestClient emits one deprecation warning about its HTTPX
test adapter. It does not fail the tests; application HTTP calls use HTTPX directly.

## Pending verification

`docker info` could not connect to `//./pipe/docker_engine`: the Docker daemon was
unavailable. Image build, container networking, container health, Compose node
disconnect/recovery, and container execution remain unverified. Follow the README
Compose smoke procedure when Docker is running. Local-process success does not
establish container success. The Docker Python 3.12 runtime also remains untested.

No Kubernetes deployment or real-road/hardware execution was attempted. This is
an ephemeral, trusted-local simulation, not a production recovery guarantee.
