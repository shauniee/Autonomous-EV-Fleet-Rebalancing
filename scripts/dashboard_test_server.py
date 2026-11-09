"""Isolated real backend for Playwright; no existing service state is touched."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
from isolated_test_environment import service_test_environment

ROOT = Path(__file__).resolve().parents[1]
children = []


def main():
    env = service_test_environment(
        ROOT, FLEET_URL="http://127.0.0.1:18001", DEMAND_URL="http://127.0.0.1:18002",
        REBALANCING_URL="http://127.0.0.1:18003", DISPATCH_URL="http://127.0.0.1:18004",
        FACILITIES_URL="http://127.0.0.1:18006", ROUTING_PROVIDER="ONEMAP",
        ONEMAP_ACCESS_TOKEN="offline-test-fixture", RECOMMENDATION_TTL_SECONDS="10",
    )
    logs = ROOT / "dashboard" / "test-results" / "backend"
    logs.mkdir(parents=True, exist_ok=True)
    handles = []

    def start(name, args):
        handle = (logs / f"{name}.log").open("w", encoding="utf-8")
        handles.append(handle)
        children.append(subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT))

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            # Refuse to reuse a server belonging to a different run.
            import socket
            for port in range(18001, 18009):
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", port))
            services = [
                ("fleet", 18001, "ev_fleet.fleet.app:app"),
                ("demand", 18002, "ev_fleet.demand.app:app"),
                ("rebalancing", 18003, "ev_fleet.rebalancing.app:app"),
                ("dispatch", 18004, "tests.stubbed_dispatch_app:app"),
                ("facilities", 18006, "ev_fleet.facilities.app:app"),
            ]
            for name, port, module in services:
                start(name, ["-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)])
            deadline = time.monotonic() + 40
            for _, port, _ in services:
                while True:
                    try:
                        client.get(f"http://127.0.0.1:{port}/health/ready").raise_for_status()
                        break
                    except httpx.HTTPError:
                        if time.monotonic() > deadline:
                            raise RuntimeError("Backend startup timed out; inspect test-results/backend")
                        time.sleep(.2)
            zone_url = "http://127.0.0.1:18002/api/v1/zones/cbd"
            zone = client.get(zone_url).json()
            client.put(zone_url + "/demand", json={"expected_version": zone["demand_version"],
                       "observed_at": datetime.now(timezone.utc).isoformat(), "desired_available_vehicles": 12}).raise_for_status()
            for health_port, (name, zone) in enumerate(
                [("ev-browser-01", "bishan"), ("ev-browser-02", "tampines")], start=18007
            ):
                start(name, ["-m", "ev_fleet.simulator.cli", "--fleet-url", env["FLEET_URL"],
                            "--dispatch-url", "http://127.0.0.1:18004", "--vehicle-id", name, "--zone", zone,
                            "--soc", "90", "--time-scale", "60", "--interval", "0.5",
                            "--health-port", str(health_port)])
            deadline = time.monotonic() + 20
            while True:
                vehicles = client.get(env["FLEET_URL"] + "/api/v1/vehicles").json()["vehicles"]
                if len(vehicles) == 2 and all(v["telemetry"] and not v["stale"] for v in vehicles):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("EV startup timed out")
                time.sleep(.2)

        class Ready(BaseHTTPRequestHandler):
            def do_GET(self):
                healthy = all(child.poll() is None for child in children)
                self.send_response(200 if healthy else 503)
                self.end_headers()
                self.wfile.write(b"ready" if healthy else b"child process stopped")

            def log_message(self, *_):
                pass

        with HTTPServer(("127.0.0.1", 18005), Ready) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
