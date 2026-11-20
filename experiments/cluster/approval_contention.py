"""N operators approve at the same instant on the live cluster (via the 8080/8090 proxies).

Each round, every operator requests advice for the same short zone and then all
approve together with their own request ID. We record each approval's latency
and outcome, then check that at most one claim exists per vehicle and that no
loser kept a claim or a parking hold.

Usage: python experiments/cluster/approval_contention.py OUT.jsonl [ROUNDS] [N ...]
       python experiments/cluster/approval_contention.py --summarise OUT.jsonl
"""
import asyncio, json, sys, time, uuid
import httpx

API, CONSOLE = "http://127.0.0.1:8080/api", "http://127.0.0.1:8090/api/probe/v1"
ZONES = ("cbd", "tampines", "punggol", "woodlands", "jurong-east", "bishan")
ACTIVE = {"PENDING", "SENT", "ACKNOWLEDGED", "IN_PROGRESS", "ARRIVAL_PENDING", "UNKNOWN"}


async def timed(client, method, url, **kw):
    start = time.perf_counter()
    r = await client.request(method, url, **kw)
    return r, round((time.perf_counter() - start) * 1000, 1)


async def set_target(client, target):
    for z in ZONES:  # every other zone wants nothing, so a move never hurts its donor
        r = await client.put(f"{CONSOLE}/zones/{z}/demand", json={"desired_available_vehicles": 20 if z == target else 0})
        r.raise_for_status()


async def wait_for_capacity(client, limit=2):
    # Dispatch admits at most 4 active journeys; keep earlier winners from refusing new approvals.
    while True:
        d = (await client.get(API + "/dispatch/dispatches")).json()["dispatches"]
        if sum(x["state"] in ACTIVE for x in d) <= limit:
            return
        await asyncio.sleep(2)


async def round_(client, n, target):
    gate, ready, released = asyncio.Event(), [0], [0.0]

    async def operator():
        rec, rec_ms = {}, None
        try:
            r, rec_ms = await timed(client, "POST", API + "/rebalancing/recommendations", json={"target_zone_id": target})
            rec = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            rec = {"decision": type(exc).__name__}
        finally:
            ready[0] += 1
            if ready[0] == n:  # the last operator to get advice releases everyone at once
                released[0] = time.perf_counter()
                gate.set()
        await gate.wait()
        if not rec.get("selected_vehicle_id"):  # HOLD, DEFER or an error: nothing to approve
            return {"rec_ms": rec_ms, "decision": (rec.get("crds") or {}).get("decision") or rec.get("decision"),
                    "skipped": True}
        request_id = str(uuid.uuid4())
        r, ms = await timed(client, "POST", API + "/dispatch/dispatches",
                            json={"request_id": request_id, "recommendation_id": rec["recommendation_id"]})
        body = r.json()
        return {"rec_ms": rec_ms, "vehicle": rec.get("selected_vehicle_id"), "recommendation_id": rec["recommendation_id"],
                "request_id": request_id, "http": r.status_code, "approve_ms": ms,
                "approval": body.get("approval_status"), "state": body.get("state"),
                "code": (body.get("error") or {}).get("code") or body.get("failure_reason")}

    ops = await asyncio.gather(*(operator() for _ in range(n)))
    makespan = round((time.perf_counter() - released[0]) * 1000, 1)
    # Invariants: one Fleet claim per vehicle, losers hold nothing.
    winners = [o for o in ops if o.get("approval") == "APPROVED"]
    claims, leaks = {}, []
    for vid in {o["vehicle"] for o in ops if o.get("vehicle")}:
        v = (await client.get(f"{API}/fleet/vehicles/{vid}")).json()
        claims[vid] = (v.get("active_claim") or {}).get("dispatch_id")
    for o in ops:
        if o.get("request_id") and o.get("approval") != "APPROVED":
            a = (await client.get(f"{API}/dispatch/approvals/{o['request_id']}")).json()
            if a.get("claim_held") or a.get("parking_status") == "RESERVED":
                leaks.append(o["request_id"])
    winner_ids = {(await client.get(f"{API}/dispatch/approvals/{w['request_id']}")).json().get("dispatch_id") for w in winners}
    return {"operators": n, "target": target, "winners": len(winners),
            "distinct_vehicles": len(claims), "distinct_recommendations": len({o.get("recommendation_id") for o in ops}),
            "claims_match_winners": all(c is None or c in winner_ids for c in claims.values()) and
                                    sum(c is not None and c in winner_ids for c in claims.values()) == len(winners),
            "leaked_losers": leaks, "makespan_ms": makespan, "ops": ops}


def summarise(rows):
    out = {}
    for n in sorted({r["operators"] for r in rows}):
        rs = [r for r in rows if r["operators"] == n]
        ops = [o for r in rs for o in r["ops"] if "approve_ms" in o]
        win = [o["approve_ms"] for o in ops if o["approval"] == "APPROVED"]
        lose = [o["approve_ms"] for o in ops if o["approval"] != "APPROVED"]
        q = lambda xs, p: round(sorted(xs)[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else None
        out[n] = {"rounds": len(rs), "one_winner_rounds": sum(r["winners"] == 1 for r in rs),
                  "double_claims": sum(not r["claims_match_winners"] for r in rs),
                  "leaked_losers": sum(len(r["leaked_losers"]) for r in rs),
                  "winner_ms_median": q(win, 0.5), "loser_ms_median": q(lose, 0.5),
                  "approve_ms_p95": q(win + lose, 0.95), "makespan_ms_median": q([r["makespan_ms"] for r in rs], 0.5),
                  "loser_codes": sorted({o["code"] for o in ops if o["approval"] != "APPROVED"}, key=str)}
    return out


async def main(path, rounds, levels):
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        zones = (await client.get(API + "/demand/zones")).json()
        zones = zones.get("zones", zones)
        original = {z["zone_id"]: (z.get("demand") or {}).get("desired_available_vehicles") for z in zones}
        (await client.post(CONSOLE + "/demand-simulation/pause")).raise_for_status()
        rows, i = [], 0
        try:
            with open(path, "a") as out:
                for n in levels:
                    for _ in range(rounds):
                        target = ZONES[i % len(ZONES)]; i += 1
                        await wait_for_capacity(client)
                        await set_target(client, target)
                        await asyncio.sleep(2)
                        row = await round_(client, n, target)
                        rows.append(row)
                        out.write(json.dumps(row) + "\n"); out.flush()
                        print(n, target, row["winners"], row["claims_match_winners"], row["makespan_ms"], flush=True)
        finally:
            for z, d in original.items():
                if d is not None:
                    await client.put(f"{CONSOLE}/zones/{z}/demand", json={"desired_available_vehicles": d})
        print(json.dumps(summarise(rows), indent=1))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[0] == "--summarise":
        print(json.dumps(summarise([json.loads(line) for line in open(args[1])]), indent=1))
    else:
        asyncio.run(main(args[0], int(args[1]) if len(args) > 1 else 5,
                         [int(a) for a in args[2:]] or [1, 2, 4, 8]))
