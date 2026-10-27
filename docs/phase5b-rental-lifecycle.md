# Phase 5B — Simulated free-floating rental lifecycle

Phase 5B extends Fleet with safe, simulation-only customer rental **start and return
events**. Customers can return at any location inside the current simulation
coordinate envelope; the actual GPS coordinate and battery are obtained from
fresh, ordered **Fleet telemetry** rather than a guessed end destination.

## Implemented

- POST `/api/v1/vehicles/{vehicle_id}/rentals` with a caller-supplied rental
  UUID, idempotency request UUID, latest `expected_version`, and future
  `expected_end_at` (maximum 30 days out).
- GET `/api/v1/vehicles/{vehicle_id}/rentals/{rental_id}`.
- POST `/api/v1/vehicles/{vehicle_id}/rentals/{rental_id}/return` with
  idempotency request UUID and latest `expected_version`.
- `active_rental` on Fleet vehicle snapshots, and visible rental indicators
  on the dispatcher Fleet table/map.
- Rental start and vehicle claiming serialised by the existing Fleet lock.
  Both Rebalancing's eligibility assessment and Fleet's final claim check
  prohibit dispatch of rented cars, including when simulator telemetry
  continues reporting the underlying EV as AVAILABLE.
- Confirmed return requires **new telemetry since rental start**, from the
  current valid telemetry session, no active relocation or claim, and fresh
  connectivity. The accepted telemetry determines return position and SOC.
- A retry with the same request body returns the original receipt; conflicting
  IDs/payloads and stale versions return 409.

## Example (PowerShell)

Use an isolated demo EV that is **not** simultaneously controlled by a
stationary or moving simulator process. Other simulator nodes continue sending
their own authoritative telemetry, so manually rewriting their sessions would
create a competing physical-state model.

1. Register a fresh test EV through the Fleet API (port 8001).
2. Open a telemetry session for it and POST telemetry with
   `availability=AVAILABLE`, `relocation_state=IDLE`, a valid
   observed timestamp, and an initial position.
3. Read the vehicle's `state_version` and start a rental:

```powershell
$vehicle = Invoke-RestMethod http://127.0.0.1:8001/api/v1/vehicles/ev-rental-demo
$rentalId = [guid]::NewGuid().ToString()
$start = @{
  rental_id = $rentalId
  request_id = [guid]::NewGuid().ToString()
  expected_version = $vehicle.state_version
  expected_end_at = [DateTimeOffset]::UtcNow.AddHours(2).ToString('o')
} | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType application/json -Body $start `
  -Uri http://127.0.0.1:8001/api/v1/vehicles/ev-rental-demo/rentals
```

4. Simulate the customer's free-floating journey by sending a **new, ordered
   telemetry observation** in the same session, with any legal simulated
   Singapore GPS coordinates and its actual remaining SOC. Fleet continues
   to mark the car unavailable while rented.
5. Read the latest `state_version`, then confirm return:

```powershell
$vehicle = Invoke-RestMethod http://127.0.0.1:8001/api/v1/vehicles/ev-rental-demo
$return = @{
  request_id = [guid]::NewGuid().ToString()
  expected_version = $vehicle.state_version
} | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType application/json -Body $return `
  -Uri "http://127.0.0.1:8001/api/v1/vehicles/ev-rental-demo/rentals/$rentalId/return"
```

Fleet records the last telemetry position and SOC as the confirmed return.
The returned EV becomes eligible **only if** the most recent telemetry also
reports availability and IDLE state and still passes existing policy checks.
This does not automatically dispatch or reserve a parking space.

## Limitations and next phases

- No customer authentication, rental payments, reservation system or real
  physical EV integration. Protect simulated rental mutation endpoints from
  untrusted external clients.
- Rental records and deduplication are **in-memory only**; restarting Fleet
  loses them. Keep one Fleet process/Pod until shared persistent storage is
  implemented.
- The existing moving EV simulator does not yet drive a rental journey or
  automatically complete a rental. This phase exposes validated Fleet APIs
  and read-only dashboard status to support a future rental simulator.
- No pre-dispatch scheduling, multi-EV batches, parking allocation or charger
  ranking yet. These need durable, exclusive claims and return-time checks.
- Existing Phase 1–4 selection weights and evaluation output are unchanged.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_rentals.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd dashboard
npm.cmd test -- --run
npm.cmd run build
```
