"""Four real Uvicorn processes plus independent moving EV processes over loopback HTTP."""
import os
import socket
import subprocess
import sys
import time
from uuid import uuid4
import httpx
import pytest


@pytest.fixture
def relocation_cluster(tmp_path):
    sockets = [socket.socket() for _ in range(4)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    urls = [f"http://127.0.0.1:{port}" for port in ports]
    env = dict(os.environ, FLEET_URL=urls[0], DEMAND_URL=urls[1], REBALANCING_URL=urls[2],
               RECONCILE_INTERVAL_SECONDS="0.1", STALE_SECONDS="5", OFFLINE_SECONDS="10",
               RECOMMENDATION_TTL_SECONDS="10", ACK_TIMEOUT_SECONDS="3", PROGRESS_TIMEOUT_SECONDS="5")
    processes, logs = [], []
    def start(name, args):
        log = open(tmp_path / f"{name}.log", "w")
        logs.append(log)
        process = subprocess.Popen([sys.executable, *args], env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(process)
        return process
    try:
        for service, port in zip(("fleet", "demand", "rebalancing", "dispatch"), ports):
            start(service, ["-m", "uvicorn", f"ev_fleet.{service}.app:app", "--host", "127.0.0.1", "--port", str(port)])
        with httpx.Client(timeout=2, trust_env=False) as client:
            deadline = time.monotonic() + 20
            for url in urls:
                while True:
                    try:
                        if client.get(url + "/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline:
                        pytest.fail("Startup failed: " + "\n".join(path.read_text() for path in tmp_path.glob("*.log")))
                    time.sleep(.05)
            for name, zone, soc in [("moving-01", "bishan", "90"), ("moving-02", "tampines", "60")]:
                start(name, ["-m", "ev_fleet.simulator.cli", "--fleet-url", urls[0], "--dispatch-url", urls[3],
                    "--vehicle-id", name, "--zone", zone, "--soc", soc, "--interval", ".15",
                    "--poll-interval", ".1", "--tick-seconds", ".05", "--time-scale", "240"])
            yield client, urls, tmp_path
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()


def test_explicit_approval_gradual_relocation_and_return_to_available(relocation_cluster):
    client, (fleet, demand, rebalancing, dispatch_url), log_path = relocation_cluster
    deadline = time.monotonic() + 10
    while True:
        vehicles = client.get(fleet + "/api/v1/vehicles").json()["vehicles"]
        if len(vehicles) == 2 and all(v["telemetry"] and not v["stale"] for v in vehicles):
            break
        assert time.monotonic() < deadline, "EV registration timed out"
        time.sleep(.05)
    rec = client.post(rebalancing + "/api/v1/recommendations", json={"target_zone_id": "cbd"}).json()
    vehicle_id = rec["selected_vehicle_id"]
    vehicle_url = fleet + "/api/v1/vehicles/" + vehicle_id
    before = client.get(vehicle_url).json()
    time.sleep(.3)
    after = client.get(vehicle_url).json()
    assert after["telemetry"]["position"] == before["telemetry"]["position"]
    assert after["telemetry"]["soc_pct"] == before["telemetry"]["soc_pct"]
    assert after["active_claim"] is None
    body = {"request_id": str(uuid4()), "recommendation_id": rec["recommendation_id"]}
    response = client.post(dispatch_url + "/api/v1/dispatches", json=body)
    assert response.status_code == 202, response.text
    record = response.json()
    status_url = dispatch_url + "/api/v1/dispatches/" + record["dispatch_id"]
    assert client.post(dispatch_url + "/api/v1/dispatches", json=body).json()["dispatch_id"] == record["dispatch_id"]
    positions, socs, states = [], [], []
    deadline = time.monotonic() + 20
    while True:
        state = client.get(vehicle_url).json()
        positions.append(state["telemetry"]["position"]["latitude"])
        socs.append(state["telemetry"]["soc_pct"])
        result = client.get(status_url).json()
        states.append(result["state"])
        if result["state"] == "SUCCEEDED" and state["effective_availability"] == "AVAILABLE":
            break
        assert time.monotonic() < deadline, (result, "\n".join(path.read_text()[-2000:] for path in log_path.glob("*.log")))
        time.sleep(.08)
    assert "IN_PROGRESS" in states
    assert len(set(positions)) >= 3
    assert all(a >= b for a, b in zip(socs, socs[1:]))
    assert socs[-1] < before["telemetry"]["soc_pct"]
    assert state["current_zone_id"] == "cbd"
    assert state["telemetry"]["position"] == record["command"]["target_position"]
    assert state["active_claim"] is None
    assert result["claim_held"] is False and result["arrival_soc_pct"] is not None
