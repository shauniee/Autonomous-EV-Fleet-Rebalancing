# Mock parking and charging facilities

The mock catalogue contains 15 fictional locations near the six simulated
Singapore zone centres. For provider-backed observations see [LTA DataMall](lta-datamall.md).
Both sources support simulated allocation.

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

[CRDS](crds.md) reads facility freshness, parking and charger capacity before
requesting road evidence. Dispatch rechecks the selected facility at approval
and acquires a simulated hold before the Fleet claim. Verified arrival can
turn that hold into occupancy. See [multi-EV allocation](multi-ev-parking-allocation.md)
for the hold lifecycle.

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
The dashboard identifies the facility data source. Recommendation previews
only read capacity. Approval can change simulated availability.

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

Available-space observations are planning inputs. Facility holds reserve
capacity inside the simulation only.
