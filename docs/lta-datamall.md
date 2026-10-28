# LTA DataMall integration

Real parking and charging observations supply destinations for simulated EVs.
They do not reserve public spaces, operate chargers or provide road routing.

## Enable on macOS

Add these entries to the existing repository-root `.env`:

```dotenv
FACILITIES_SOURCE=LTA_DATAMALL
LTA_DATAMALL_ACCOUNT_KEY=your-account-key
```

With Docker Desktop Kubernetes using **kubeadm**, run from the repository root:

```bash
bash scripts/start-kubernetes.sh
```

Docker, kubectl, Python 3 and curl must be available. The script builds three
images, loads whitelisted facilities settings into the `ev-fleet-facilities`
Secret, deploys the stack and opens both dashboard tunnels. It restarts only
Facilities, Rebalancing, Dispatch and Dashboard to load this integration.
Run it between demos, without active relocations. Existing simulator Pods,
Fleet, the simulation console, PostgreSQL and Redis are not explicitly restarted.
AMD64 images use emulation on Apple Silicon, matching the existing deployment.

Open Fleet Dispatch at http://127.0.0.1:8080 and the simulation console at
http://127.0.0.1:8090. Keep the terminal open; Ctrl+C closes only the tunnels.
Set demand in the simulation console, select that zone in Fleet Dispatch,
request 1–20 EVs, review their destinations and approve the relocation.
Live mode uses facility destinations even for quantity one. Legacy single-EV
HTTP endpoints remain zone-based.

Compose reads the same `.env` settings. Direct Python execution requires exported
environment variables. For manual Kubernetes or Windows deployment, load the
Secret separately and restart Facilities after changing it:

```bash
python3 scripts/render_facilities_secret.py .env | kubectl --context docker-desktop apply -f -
kubectl --context docker-desktop -n ev-fleet rollout restart deployment/facilities
```

Use `python` on Windows if needed. Do not save or print the renderer's output:
it contains the key. The optional Secret is referenced only by Facilities;
ordinary `kubectl apply -k k8s` does not read `.env`. Keys are never included in
frontend settings or image builds. Set `FACILITIES_SOURCE=MOCK` for offline demos.

## Feed and simulation rules

Reference: [LTA API User Guide v6.10](https://datamall.lta.gov.sg/content/dam/datamall/datasets/LTA_DataMall_API_User_Guide.pdf),
sections 1, 2.12, 2.28 and 2.29. API base:
`https://datamall2.mytransport.sg/ltaodataservice`.

| Feed | Endpoint | Refresh | Maximum age |
|---|---|---|---|
| Car parking | `CarParkAvailabilityv2`, paginated with `$skip` in batches of 500 | 60 seconds | 180 seconds |
| Charging | `EVCBatch`, followed by its signed JSON download | 300 seconds | 900 seconds |

- Only DataMall requests carry `AccountKey`; the HTTPS S3 download does not.
  Refreshes run independently with bounded timeouts and pagination.
- Car parking includes only `C` records with usable IDs, counts and coordinates.
  Total parking capacity and associated charging facilities remain unknown.
- Charging locations stay separate from carparks: no shared identifier is supplied.
  Individual connector statuses determine availability; station status is not enough.
  One simulated vehicle per charger is a conservative assumption, not a claim
  about physical bays or simultaneous sessions. Connector compatibility and
  battery replenishment are not modelled. High-SOC cars do not occupy chargers.
- Each real coordinate is assigned to the nearest existing operating zone;
  this does not represent official geographic boundaries. Unknown total capacity
  receives neutral parking utility rather than a fabricated occupancy ratio.
- Reported availability stays separate from local simulated holds. Holds survive
  refreshes and subtract from available simulation slots, clamped at zero.
  PostgreSQL snapshots preserve observations and holds under the existing
  single-replica constraint; no database migration is required.
- Fetch timestamps are local retrieval times. Failed refreshes retain the original
  expiry. Expired locations show `UNKNOWN` and cannot receive new allocations.
  The dashboard reports each feed's freshness/errors; live mode never silently
  substitutes mock data. Availability does not guarantee a real space on arrival.

Authenticated API responses and actual Kubernetes startup remain unverified until
the key is supplied. Provider fixtures are illustrative schema-based records;
batch JSON currently supports a list or a `value` array.
