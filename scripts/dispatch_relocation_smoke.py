"""Approve one simulated journey on an already-running backend and capture evidence."""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-port", type=int, default=8001)
    parser.add_argument("--target", default="cbd")
    parser.add_argument("--timeout", type=float, default=150)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fleet, demand, rebalancing, dispatch = [f"http://127.0.0.1:{args.base_port + offset}" for offset in range(4)]
    evidence = {"services": [fleet, demand, rebalancing, dispatch], "samples": []}
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    with httpx.Client(timeout=3, trust_env=False) as client:
        def request(method, url, body=None):
            response = client.request(method, url, json=body)
            response.raise_for_status()
            return response.json()
        try:
            evidence["health"] = [request("GET", url + "/health/ready") for url in evidence["services"]]
            deadline = time.monotonic() + 15
            while True:
                vehicles = request("GET", fleet + "/api/v1/vehicles")["vehicles"]
                if len(vehicles) >= 2 and all(v["telemetry"] and not v["stale"] for v in vehicles):
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("Expected two fresh moving nodes")
                time.sleep(.2)
            rec = request("POST", rebalancing + "/api/v1/recommendations", {"target_zone_id": args.target})
            evidence["recommendation"] = rec
            assert rec["selected_vehicle_id"], "No eligible EV"
            vehicle_url = fleet + "/api/v1/vehicles/" + rec["selected_vehicle_id"]
            before = request("GET", vehicle_url)
            time.sleep(.4)
            unchanged = request("GET", vehicle_url)
            assert before["telemetry"]["position"] == unchanged["telemetry"]["position"]
            assert before["telemetry"]["soc_pct"] == unchanged["telemetry"]["soc_pct"]
            assert unchanged["active_claim"] is None
            evidence["before_approval"] = before
            body = {"request_id": str(uuid4()), "recommendation_id": rec["recommendation_id"]}
            approved = request("POST", dispatch + "/api/v1/dispatches", body)
            evidence["approval"] = approved
            assert approved["dispatch_id"] == request("POST", dispatch + "/api/v1/dispatches", body)["dispatch_id"]
            status_url = dispatch + "/api/v1/dispatches/" + approved["dispatch_id"]
            save()
            print(json.dumps({"approved": approved["dispatch_id"], "vehicle_id": rec["selected_vehicle_id"]}), flush=True)
            deadline = time.monotonic() + args.timeout
            last_state = None
            while True:
                state = request("GET", vehicle_url)
                outcome = request("GET", status_url)
                evidence["samples"].append({"vehicle": state, "dispatch": outcome})
                if outcome["state"] != last_state:
                    print(json.dumps({"state": outcome["state"], "soc": state["telemetry"]["soc_pct"],
                                      "claim_held": outcome["claim_held"]}), flush=True)
                    last_state = outcome["state"]
                if outcome["state"] == "SUCCEEDED" and state["effective_availability"] == "AVAILABLE":
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("Journey did not complete within timeout")
                time.sleep(.5)
            positions = {tuple(sorted(sample["vehicle"]["telemetry"]["position"].items())) for sample in evidence["samples"]}
            assert len(positions) >= 3, "Expected gradual movement"
            assert state["telemetry"]["soc_pct"] < before["telemetry"]["soc_pct"]
            assert state["current_zone_id"] == args.target and state["active_claim"] is None
            assert state["telemetry"]["position"] == approved["command"]["target_position"]
            evidence["passed"] = True
            print(json.dumps({"passed": True, "positions_observed": len(positions),
                              "soc_start": before["telemetry"]["soc_pct"], "soc_end": state["telemetry"]["soc_pct"],
                              "distance_km": outcome["distance_travelled_km"]}), flush=True)
        except Exception as exc:
            evidence["passed"] = False
            evidence["error"] = str(exc)
            raise
        finally:
            save()


if __name__ == "__main__":
    main()
