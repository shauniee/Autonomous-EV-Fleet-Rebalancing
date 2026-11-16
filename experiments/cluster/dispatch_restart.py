"""Kill the Dispatch pod mid-journey and measure recovery on the live cluster (via the 8080/8090 proxies)."""
import json, subprocess, sys, time, uuid
import httpx
API, CONSOLE = "http://127.0.0.1:8080/api", "http://127.0.0.1:8090/api/probe/v1"
c = httpx.Client(timeout=3, trust_env=False)
out = open(sys.argv[1], "a")
trials = int(sys.argv[2]) if len(sys.argv) > 2 else 5

def get(url):
    try:
        r = c.get(url); return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else None)
    except httpx.HTTPError as e:
        return None, type(e).__name__

def ready_pod():
    q = subprocess.run(["kubectl", "-n", "ev-fleet", "get", "pods", "-l", "app.kubernetes.io/name=dispatch", "-o",
                        "jsonpath={range .items[*]}{.metadata.name} {.metadata.deletionTimestamp} {.status.conditions[?(@.type=='Ready')].status}\n{end}"],
                       capture_output=True, text=True).stdout.split("\n")
    return [l.split()[0] for l in q if l.strip() and l.split()[-1] == "True" and len(l.split()) == 2]

c.post(CONSOLE + "/demand-simulation/pause")
for z in ("jurong-east", "woodlands", "bishan", "punggol", "tampines", "cbd"):
    c.put(f"{CONSOLE}/zones/{z}/demand", json={"desired_available_vehicles": 12 if z == "cbd" else 0})
for trial in range(trials):
    time.sleep(5)
    rec = c.post(API + "/rebalancing/recommendations", json={"target_zone_id": "cbd"}).json()
    if not rec.get("selected_vehicle_id"):
        out.write(json.dumps({"trial": trial, "error": "no move", "rec": {k: rec.get(k) for k in ("decision", "reason_code", "error")}}) + "\n"); out.flush(); continue
    vid = rec["selected_vehicle_id"]
    approved = c.post(API + "/dispatch/dispatches", json={"request_id": str(uuid.uuid4()), "recommendation_id": rec["recommendation_id"]}).json()
    did = approved.get("dispatch_id")
    if not did:
        out.write(json.dumps({"trial": trial, "error": "approval", "body": approved}) + "\n"); out.flush(); continue
    durl, vurl = f"{API}/dispatch/dispatches/{did}", f"{API}/fleet/vehicles/{vid}"
    t_approve = time.monotonic()
    while True:  # wait until mid-journey
        _, d = get(durl)
        if d and d.get("state") == "IN_PROGRESS": break
        if time.monotonic() - t_approve > 120: break
        time.sleep(0.5)
    time.sleep(3)
    old = ready_pod()
    t_kill = time.monotonic()
    subprocess.run(["kubectl", "-n", "ev-fleet", "delete", "pod", "-l", "app.kubernetes.io/name=dispatch", "--wait=false"], capture_output=True)
    samples, first_ok, pod_ready, claim_lost, states = [], None, None, False, []
    while time.monotonic() - t_kill < 600:
        now = time.monotonic() - t_kill
        code, d = get(durl); vcode, v = get(vurl)
        if code == 200 and first_ok is None and now > 1: first_ok = now
        if pod_ready is None and set(ready_pod()) - set(old): pod_ready = now
        claim = isinstance(v, dict) and v.get("active_claim")
        if vcode == 200 and not claim and not (isinstance(d, dict) and d.get("state") == "SUCCEEDED"): claim_lost = True
        st = d.get("state") if isinstance(d, dict) else None
        if st and (not states or states[-1] != st): states.append(st)
        samples.append({"t": round(now, 2), "dispatch_http": code, "state": st,
                        "pos": (v.get("telemetry") or {}).get("position") if isinstance(v, dict) else None})
        if st in ("SUCCEEDED", "FAILED", "REJECTED"): break
        time.sleep(0.25)
    moved = len({json.dumps(s["pos"]) for s in samples if s["pos"] and (first_ok is None or s["t"] < first_ok)})
    out.write(json.dumps({"trial": trial, "vehicle": vid, "dispatch_id": did, "api_recovered_s": first_ok,
                          "pod_ready_s": pod_ready, "final_state": states[-1] if states else None, "states": states,
                          "finished_s": samples[-1]["t"], "claim_released_early": claim_lost,
                          "positions_during_outage": moved,
                          "outage_http": sorted({str(s["dispatch_http"]) for s in samples if first_ok and s["t"] < first_ok})}) + "\n")
    out.flush()
