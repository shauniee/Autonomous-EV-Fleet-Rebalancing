#!/usr/bin/env python3
"""Preview/abandon stranded Docker Desktop simulator journeys; no local packages needed.

Run from any directory with Python 3.10+ and kubectl. See the Second Brain
"Stale Kubernetes Journey Recovery" guide before using --apply.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


SERVICES = ("dispatch", "fleet", "facilities")
NAMESPACES = ("fleet", "dispatch", "facilities")
TERMINAL = {"SUCCEEDED", "REJECTED", "FAILED"}
REASON = "MANUAL_STALE_JOURNEY_ABANDONED"


def busy(record):
    telemetry = record.get("telemetry")
    return bool(record.get("active_claim") or record.get("active_rental")
                or record.get("active_charging_task") or telemetry and (
                    telemetry.active_dispatch_id or telemetry.relocation_state != "IDLE"
                    or telemetry.charging_state == "CHARGING"))


def repair(snapshots, vehicle_ids):
    """Validate the entire maintenance window, then change only selected EV work."""
    snapshots = deepcopy(snapshots)
    fleet, dispatch, facilities = (snapshots[name] for name in NAMESPACES)
    selected = set(vehicle_ids)
    now = datetime.now(timezone.utc)
    for vehicle_id, record in fleet["vehicles"].items():
        if vehicle_id not in selected and busy(record):
            raise ValueError(f"Other active work: {vehicle_id}. Wait for it to finish.")
    for entry in dispatch["records"].values():
        data = entry["data"]
        if data["state"] not in TERMINAL and data["vehicle_id"] not in selected:
            raise ValueError(f"Other nonterminal dispatch: {data['dispatch_id']}")
    report = []
    changed = set()
    for vehicle_id in vehicle_ids:
        record = fleet["vehicles"][vehicle_id]
        telemetry = record.get("telemetry")
        claim = record.get("active_claim")
        if not telemetry or not busy(record):
            raise ValueError(f"{vehicle_id}: no recorded stranded journey")
        if (record.get("active_rental") or record.get("active_charging_task")
                or telemetry.charging_state == "CHARGING"):
            raise ValueError(f"{vehicle_id}: rental/charging recovery is unsupported")
        if (now - record["received_at"]).total_seconds() < 120:
            raise ValueError(f"{vehicle_id}: telemetry must be at least 120 seconds old")
        if telemetry.gps_quality != "SIMULATED":
            raise ValueError(f"{vehicle_id}: not verified simulated telemetry")
        ids = {key for key, entry in dispatch["records"].items()
               if entry["data"]["vehicle_id"] == vehicle_id
               and (entry["data"]["state"] not in TERMINAL or entry["data"]["claim_held"])}
        if claim:
            ids.add(str(claim["dispatch_id"]))
            if claim["vehicle_id"] != vehicle_id or str(claim["fleet_instance_id"]) != str(fleet["instance_id"]):
                raise ValueError(f"{vehicle_id}: claim identity mismatch")
        if telemetry.active_dispatch_id:
            ids.add(str(telemetry.active_dispatch_id))
        if len(ids) != 1:
            raise ValueError(f"{vehicle_id}: expected exactly one linked dispatch, found {sorted(ids)}")
        dispatch_id = ids.pop()
        entry = dispatch["records"][dispatch_id]
        data = entry["data"]
        command = data["command"]
        if (data["vehicle_id"] != vehicle_id or not command
                or command["vehicle_id"] != vehicle_id
                or str(command["dispatch_id"]) != dispatch_id
                or str(command["fleet_instance_id"]) != str(fleet["instance_id"])
                or data["state"] == "SUCCEEDED"):
            raise ValueError(f"{vehicle_id}: inconsistent/already successful dispatch")
        for key, history in record["claims"].items():
            receipt = history["receipt"]
            if receipt["status"] == "ACTIVE" and key != dispatch_id:
                raise ValueError(f"{vehicle_id}: conflicting active claim")
        if claim:
            history = record["claims"][dispatch_id]["receipt"]
            if history != claim or str(claim["command_id"]) != str(command["command_id"]):
                raise ValueError(f"{vehicle_id}: claim/command/history mismatch")
            # Existing receipt schema has only ARRIVED/REJECTED release outcomes.
            # FAILED + REASON below records the explicit maintenance abandonment.
            history.update(status="RELEASED", outcome="REJECTED", released_at=now.isoformat())
        elif any(h["receipt"]["status"] == "ACTIVE" for h in record["claims"].values()):
            raise ValueError(f"{vehicle_id}: active claim missing from vehicle")
        record["active_claim"] = None
        record["telemetry"] = type(telemetry).model_validate({**telemetry.model_dump(mode="json"),
            "relocation_state": "IDLE", "active_dispatch_id": None,
            "distance_travelled_km": None, "availability": "UNAVAILABLE",
            "speed_kph": 0.0, "power_kw": 0.0, "parked_facility_id": None,
            "charging_state": "NOT_CHARGING", "charging_power_kw": None})
        # Fence the previous telemetry session; only a new session can become fresh.
        if record["session_request_id"]:
            record["retired_requests"].add(record["session_request_id"])
        record["session_request_id"] = record["session_id"] = None
        record["state_version"] += 1
        record["control_version"] = record.get("control_version", record["state_version"] - 1) + 1
        fleet["version"] += 1
        for key, hold in facilities["holds"].items():
            if hold["receipt"]["vehicle_id"] == vehicle_id and hold["status"] in ("RESERVED", "OCCUPIED"):
                if hold["status"] == "RESERVED" and key != dispatch_id:
                    raise ValueError(f"{vehicle_id}: unrelated destination reservation")
                hold["status"] = hold["receipt"]["status"] = "RELEASED"
                facilities["version"] += 1
        data.update(state="FAILED", failure_reason=REASON, claim_held=False,
                    updated_at=now.isoformat(), completed_at=now.isoformat(),
                    parking_status="RELEASED" if data.get("parking_status") else None)
        entry["release_outcome"] = None
        changed.add(dispatch_id)
        report.append({"vehicle_id": vehicle_id, "dispatch_id": dispatch_id,
                       "position": telemetry.position.model_dump(mode="json"),
                       "soc_pct": telemetry.soc_pct, "result": "FAILED", "reason": REASON})
    for batch in dispatch["batches"].values():
        for item in batch["items"]:
            if item.get("dispatch_id") in changed:
                item.update(state="FAILED", error=REASON)
    return snapshots, report


async def worker(args):
    """Runs inside the existing rebalancing container, using its installed codec."""
    import asyncpg
    from ev_fleet.common.persistence import decode_state, encode_state

    connection = await asyncpg.connect(os.environ["DATABASE_URL"], command_timeout=20)
    try:
        async with connection.transaction(readonly=not args.write):
            rows = await connection.fetch(
                "SELECT namespace, payload, revision FROM service_snapshots "
                "WHERE namespace = ANY($1::text[]) ORDER BY namespace" +
                (" FOR UPDATE" if args.write else ""), list(NAMESPACES))
            raw = {r["namespace"]: {"revision": r["revision"], "payload": json.loads(r["payload"])
                                     if isinstance(r["payload"], str) else r["payload"]} for r in rows}
            if set(raw) != set(NAMESPACES):
                raise ValueError("Fleet, Dispatch and Facilities snapshots are required")
            if args.write:
                expected = json.loads(sys.stdin.read())
                if raw != expected:
                    raise ValueError("Snapshots changed after backup; refusing to write")
            repaired, report = repair({k: decode_state(v["payload"]) for k, v in raw.items()}, args.vehicle)
            if args.write:
                for name in NAMESPACES:
                    await connection.execute(
                        "UPDATE service_snapshots SET payload=$2::jsonb, revision=revision+1, "
                        "updated_at=NOW() WHERE namespace=$1",
                        name, json.dumps(encode_state(repaired[name]), allow_nan=False))
        print(json.dumps({"backup": raw, "changes": report} if not args.write else {"changes": report}))
    finally:
        await connection.close()


def main(args):
    def kubectl(*arguments, input_text=None):
        result = subprocess.run(
            ["kubectl", "--context", "docker-desktop", "-n", "ev-fleet", "--request-timeout=30s", *arguments],
            input=input_text, text=True, capture_output=True, timeout=420)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def get(*arguments):
        return json.loads(kubectl("get", *arguments, "-o", "json"))

    def check_targets():
        pods = get("pods")["items"]
        targets = []
        for vehicle_id in args.vehicle:
            matching = [p for p in pods if p["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "simulator"
                        and (p["metadata"]["name"] == vehicle_id or
                             p["metadata"].get("labels", {}).get("app.kubernetes.io/name") == vehicle_id)]
            if len(matching) != 1:
                raise ValueError(f"{vehicle_id}: expected one simulator Pod")
            pod = matching[0]
            containers = pod.get("status", {}).get("containerStatuses", [])
            if (len(containers) != 1 or containers[0].get("ready")
                    or containers[0].get("state", {}).get("waiting", {}).get("reason") != "CrashLoopBackOff"):
                raise ValueError(f"{vehicle_id}: expected a stopped container in CrashLoopBackOff; retry if it is restarting")
            logs = kubectl("logs", pod["metadata"]["name"], "--tail=100")
            if "Vehicle has an unresolved journey; refusing to overwrite its physical state" not in logs:
                logs += kubectl("logs", pod["metadata"]["name"], "--previous", "--tail=100")
            if "Vehicle has an unresolved journey; refusing to overwrite its physical state" not in logs:
                raise ValueError(f"{vehicle_id}: expected unresolved-journey startup failure")
            targets.append(pod["metadata"]["name"])
        return targets

    def check_customers():
        for kind, name in (("deployment", "customer-rental-demo"), ("job", "customer-rental-simulation")):
            value = json.loads(kubectl("get", kind, name, "--ignore-not-found", "-o", "json") or "null")
            if value and ((kind == "deployment" and value["spec"].get("replicas", 1))
                          or (kind == "job" and not any(c.get("status") == "True" and c["type"] in ("Complete", "Failed")
                                                      for c in value.get("status", {}).get("conditions", [])))):
                raise ValueError(f"Stop the customer rental workload first: {kind}/{name}")

    source = Path(__file__).read_text()
    # The source uses stdin, so the write worker receives the backup in a second
    # StringIO stream installed by a short bootstrap (no remote file is created).
    def remote(write=False, backup=None):
        command = ["exec", "-i", "deployment/rebalancing", "--", "python", "-c",
                   "import sys,json,io; d=json.load(sys.stdin); sys.argv=d['argv']; "
                   "sys.stdin=io.StringIO(d['backup']); exec(compile(d['source'],'<journey-recovery>','exec'))"]
        argv = ["recover_stale_journeys.py", "--worker"]
        for vehicle_id in args.vehicle:
            argv.extend(["--vehicle", vehicle_id])
        if write:
            argv.append("--write")
        return json.loads(kubectl(*command, input_text=json.dumps({
            "source": source, "argv": argv, "backup": json.dumps(backup)})))

    check_customers()
    targets = check_targets()
    preview = remote()
    print(json.dumps(preview["changes"], indent=2), flush=True)
    if not args.apply:
        print("PREVIEW ONLY. Add --apply to abandon these journeys during a maintenance window.")
        return
    original = {name: get("deployment", name)["spec"].get("replicas", 1) for name in SERVICES}
    if any(value != 1 for value in original.values()):
        raise ValueError("Expected one replica each for Fleet, Dispatch and Facilities")
    # Create the backup location BEFORE stopping anything; never overwrite one.
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    fd, backup_name = tempfile.mkstemp(prefix="stale-journeys-", suffix=".json", dir=backup_dir)
    stopped = []
    try:
        os.close(fd)
        print("Pausing Dispatch, Fleet and Facilities. Do not approve new work.", flush=True)
        for name in SERVICES:
            stopped.append(name)  # Restore even if kubectl's response is lost.
            kubectl("scale", f"deployment/{name}", "--replicas=0")
            kubectl("wait", "--for=delete", "pod", "-l", f"app.kubernetes.io/name={name}", "--timeout=120s")
        check_customers()
        check_targets()
        final = remote()  # Revalidate against snapshots after all writers have exited.
        with open(backup_name, "w", encoding="utf-8") as handle:
            json.dump(final["backup"], handle)
            handle.flush()
            os.fsync(handle.fileno())
        print(f"Pre-repair backup: {Path(backup_name).resolve()}", flush=True)
        print(json.dumps(remote(write=True, backup=final["backup"])["changes"], indent=2), flush=True)
    finally:
        failures = []
        for name in reversed(stopped):
            try:
                kubectl("scale", f"deployment/{name}", f"--replicas={original[name]}")
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        if failures:
            raise RuntimeError("Restore replicas manually to 1: " + "; ".join(failures))
    for name in ("facilities", "fleet", "dispatch"):
        kubectl("rollout", "status", f"deployment/{name}", "--timeout=120s")
    # The startup-crashing containers retry automatically; no Pod deletion needed.
    for pod in targets:
        kubectl("wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=360s")
    print("Recovery committed; selected simulator Pods are Ready. Reconcile pending batch approvals in the dashboard.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicle", action="append", required=True, help="Stranded EV ID; repeat for multiple EVs")
    parser.add_argument("--apply", action="store_true", help="Abandon selected journeys and update local cluster snapshots")
    parser.add_argument("--backup-dir", default=str(Path(tempfile.gettempdir()) / "ev-fleet-recovery"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--write", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.vehicle = list(dict.fromkeys(args.vehicle))
    try:
        asyncio.run(worker(args)) if args.worker else main(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Recovery stopped: {exc}", file=sys.stderr)
        sys.exit(1)
