"""Real loopback HTTP across three independent processes; no Docker substitution claim."""
import asyncio
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
import pytest
from ev_fleet.simulator.node import run_node
from scripts.isolated_test_environment import service_test_environment


@pytest.fixture
def services(tmp_path):
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    processes, logs = [], []
    env = service_test_environment(Path(__file__).resolve().parents[2],
               STALE_SECONDS="1", OFFLINE_SECONDS="2", RECOMMENDATION_TTL_SECONDS="1",
               FLEET_URL=f"http://127.0.0.1:{ports[0]}", DEMAND_URL=f"http://127.0.0.1:{ports[1]}")
    try:
        urls = [f"http://127.0.0.1:{port}" for port in ports]
        # Real Python processes can briefly be CPU-starved while Docker Desktop
        # and the complete suite share the Windows host. Overall test deadlines
        # still detect services that stop making progress.
        with httpx.Client(timeout=10, trust_env=False) as client:
            for service, port, url in zip(("fleet", "demand", "rebalancing"), ports, urls):
                log = open(tmp_path / f"{service}.log", "w")
                logs.append(log)
                process = subprocess.Popen([sys.executable, "-m", "uvicorn",
                    f"ev_fleet.{service}.app:app", "--host", "127.0.0.1", "--port", str(port),
                    "--workers", "1"], env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                processes.append(process)
                deadline = time.monotonic() + 60
                while True:
                    try:
                        if client.get(url + "/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        pytest.fail(f"{service} startup failed: " + "\n".join(
                            path.read_text() for path in tmp_path.glob("*.log")))
                    time.sleep(.05)
            yield client, urls
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


def test_three_services_two_nodes_and_staleness(services):
    client, (fleet, demand, rebalancing) = services
    async def nodes():
        await asyncio.gather(
            run_node(fleet, "near", {"latitude": 1.283, "longitude": 103.8513}, 30, updates=1),
            run_node(fleet, "far", {"latitude": 1.35, "longitude": 103.8513}, 90, updates=1))
    asyncio.run(nodes())
    body = {"expected_version": 0, "observed_at": datetime.now(timezone.utc).isoformat(),
            "desired_available_vehicles": 12}
    assert client.put(demand + "/api/v1/zones/cbd/demand", json=body).status_code == 200
    response = client.post(rebalancing + "/api/v1/recommendations", json={"target_zone_id": "cbd"})
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["selected_vehicle_id"] == "far"
    assert len(record["ranked_candidates"]) == 2
    url = rebalancing + "/api/v1/recommendations/" + record["recommendation_id"]
    assert client.get(url).status_code == 200
    time.sleep(1.1)
    assert client.get(url).status_code == 404
    expired = client.post(rebalancing + "/api/v1/recommendations", json={"target_zone_id": "cbd"}).json()
    assert expired["selected_vehicle_id"] is None
    assert all("STALE_TELEMETRY" in row["exclusion_reasons"] for row in expired["excluded_candidates"])
