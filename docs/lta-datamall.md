# LTA DataMall integration

Real parking and charging observations supply destinations for simulated EVs.
They do not reserve public spaces, operate chargers or provide road routing.

## Enable on macOS

Add these entries to the existing repository-root `.env`:

```dotenv
FACILITIES_SOURCE=LTA_DATAMALL
LTA_DATAMALL_ACCOUNT_KEY=your-account-key
```

To enable live road traffic as well, set `TRAFFIC_SOURCE=LTA_DATAMALL`.

With Docker Desktop Kubernetes using **kubeadm**, run from the repository root:

```bash
bash scripts/start-kubernetes.sh
```

Docker, kubectl, Python 3 and curl are required. Startup builds native images with
unique tags, loads whitelisted facilities settings into a Kubernetes Secret and
replaces application Pods. Run between demos without active relocations; interrupted
journeys are not reconstructed. PostgreSQL and Redis remain running.

Open Fleet Dispatch at http://127.0.0.1:8080 and the simulation console at
http://127.0.0.1:8090. Extra EV rollouts do not block the interfaces. Keep the terminal
open; healthy existing tunnels are reused and owned tunnels reconnect after Pod
changes. Ctrl+C closes only owned tunnels.

To reopen both apps **without reading `.env`, building or deploying**:

```bash
bash scripts/start-kubernetes.sh --connect-only
```

If another process occupies either port, stop its old tunnel yourself or choose
unused ports:

```bash
DASHBOARD_PORT=18080 SIMULATION_CONSOLE_PORT=18090 bash scripts/start-kubernetes.sh --connect-only
```

## End-to-end walkthrough

1. Run full startup after enabling LTA_DATAMALL. Wait for both LTA feeds to be
   FRESH and for online, available EVs to appear. Extra Pods can still be updating.
2. In the simulation console, set a valid demand target in every zone. Set CBD to 12 for the demonstration. In Fleet Dispatch, select CBD
   and confirm that real parking and charging locations are shown.
3. Choose **Parking / demand rebalancing**, quantity one, then **Recommend EV**.
   For an approvable `MOVE`, review its LTA carpark, approve and follow the relocation. For `HOLD` or `DEFER`, inspect [CRDS evidence](crds.md#decisions-batches-and-approval).
   Fleet overview should then show its `lta-parking-...` parked facility ID.
4. Choose **Charging — replenish to 80%**. Select an available EV below 80% SOC
   (or leave Any eligible EV to prioritise the lowest-SOC eligible car). Request
   and explicitly approve a recommendation to an available LTA charger. If the
   zone has no free chargers, choose another zone; no mock substitute is used.
5. After verified arrival, observe **CHARGING (simulated)** in Fleet overview,
   increasing SOC and UNAVAILABLE status. Charging uses the configured simulation time scale (10× by default in Compose
   and Kubernetes). At 80%, it reports **Charging complete · parked** and becomes available.
6. Keep that vehicle selected, switch to **Parking / demand rebalancing**, request
   a destination and approve again. On arrival at a carpark, its old local charger
   allocation is released. It consumes battery while moving to the carpark.

Live mode uses facility destinations even for quantity one. Initial EVs that have
not yet been allocated a destination are labelled Unallocated and still start at
approximate zone coordinates. Legacy single-EV HTTP endpoints remain zone-based.
Batch requests accept `purpose` (REBALANCE or CHARGE) and optional `vehicle_id`
(quantity one). Facilities exposes occupied/pending allocations at
`GET /api/v1/vehicles/{vehicle_id}/parking`.

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
  The live batch wraps stations in `evLocationsData`, omits station/charger IDs,
  and supplies postal codes plus `evCpId`. Station identities are derived from
  postal code and coordinates; connector registration prefixes identify chargers.
  One simulated vehicle per charger is conservative, not a guarantee of bays or
  simultaneous sessions. Charging uses the minimum advertised station power
  (numeric `powerRating`, or documented `chargingSpeed`), with 7.4 kW as an explicit
  fallback assumption. Rate, connector compatibility and charging taper are
  simplified; no real charger is controlled. Explicit charging requests consider
  cars below 80%; ordinary rebalancing sends low-arrival-SOC cars to chargers.
- Each real coordinate is assigned to the nearest existing operating zone;
  this does not represent official geographic boundaries. Unknown total capacity
  receives neutral parking utility rather than a fabricated occupancy ratio.
- Reported availability stays separate from local simulated holds. Holds survive
  refreshes and subtract from available simulation slots, clamped at zero.
  PostgreSQL snapshots preserve holds under the existing single-replica
  constraint. Live observations are refreshed from DataMall after a restart
  instead of being stored as a large durable snapshot; no database migration is
  required.
- Fetch timestamps are local retrieval times. Failed refreshes retain the original
  expiry. Expired locations show `UNKNOWN` and cannot receive new allocations.
  The dashboard reports each feed's freshness/errors; live mode never silently
  substitutes mock data. Availability does not guarantee a real space on arrival.

The new charging cycle awaits user-run deployment and walkthrough; tests were not
rerun. Idle simulator restarts restore reported position/SOC and allocation.
Charging pauses while Facilities cannot be verified and accrues no offline energy.
