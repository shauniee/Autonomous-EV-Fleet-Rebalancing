# Mock parking and charging facilities

This is an additive, simulation-only first step. All 15 location names, coordinates,
parking counts and charger counts are **fictional test data**, placed near the
six simulated Singapore zone centres. They must not be represented as public
carpark measurements, reservable bays, charging provider data or actual roads.

## Implemented

- Independent FastAPI Facilities Service: `ev_fleet.facilities.app:app`.
- `GET /api/v1/facilities` (optional `?zone_id=cbd`), and
  `GET /api/v1/facilities/{facility_id}`.
- Explicit `data_source: "MOCK"` on snapshots and `source: "MOCK"` on every facility.
- Validated, immutable sample catalogue: each charging bay is already included
  within parking capacity and available charging bays within available parking.
- Dashboard map markers, selected-zone facility details and independent service
  outage handling. The existing dashboard can continue without Facilities.
- Docker Compose and local Kubernetes wiring for Facilities as its own service.

The data do not update dynamically yet and no reservations are held. Neither the
EV ranking policy nor the dispatch endpoints consume Facilities data yet;
**parking and charger pins are information-only**. Actual rental returns,
multi-EV dispatch, parking-slot claiming and eventual PostgreSQL durability
are later enhancements. Reuse this API contract for a future live data adapter.

## Development

The regular relocation profile can add Facilities explicitly:

```powershell
docker compose --profile relocation up --build -d fleet demand rebalancing dispatch facilities ev-moving-01 ev-moving-02 dashboard
Invoke-RestMethod http://127.0.0.1:8005/api/v1/facilities
Invoke-RestMethod http://127.0.0.1:8005/api/v1/facilities?zone_id=cbd
```

For a local Python backend without containers:

```powershell
.\.venv\Scripts\python.exe -m uvicorn ev_fleet.facilities.app:app --host 127.0.0.1 --port 8005
```

Dashboard: `http://127.0.0.1:8080` for Compose or its configured dev port.
The Facilities data are read-only and the UI clearly labels them as mock.
Existing Fleet/Demand/Dispatch endpoints and strategy evaluation experiment results are unchanged.

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_facilities.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd dashboard
npm.cmd test -- --run
npm.cmd run build
```

To use Kubernetes, build the backend and dashboard images and apply the updated
`k8s` manifests following `docs/kubernetes-local.md`. The target is now
eight Deployments and six ClusterIP Services. Application Pods and container
runtime require verification in a Docker-enabled environment.

## Design boundary

A public "available space" is never necessarily an operator-reserved space.
Future selection must apply destination operating permissions, capacity freshness,
battery viability and exclusive fleet slot claims **at approval/execution time**.


multi-EV parking allocation extends this service with in-process mock parking reservations and
occupancy. It is no longer read-only. See `docs/multi-ev-parking-allocation.md`
for the POST hold endpoints. These are NOT reservations for real public bays.
