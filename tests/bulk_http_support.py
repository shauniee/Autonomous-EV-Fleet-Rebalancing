"""Real-process/offline-route harness restricted to disposable test databases."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import httpx

from scripts.isolated_test_environment import service_test_environment


def create_bulk_fleet_app():
    from tests.bulk_trace import install
    from ev_fleet.fleet.app import create_app
    install()
    return create_app()


def create_bulk_facilities_app():
    from ev_fleet.facilities.app import create_app
    from ev_fleet.facilities.store import FacilitiesStore
    store = FacilitiesStore()
    site = store.catalogue["mock-cbd-01"]
    store.catalogue[site.facility_id] = site.model_copy(update={
        "parking_capacity": 80, "parking_available": 80})
    return create_app(store)


def create_bulk_dispatch_app():
    from ev_fleet.dispatch import routing
    from ev_fleet.dispatch.app import create_app
    from ev_fleet.contracts.routing import RoadRoute
    from tests.routing_fixture import fixture_route

    async def offline_route(client, settings, origin, destination):
        route = fixture_route(origin, destination)
        points = [{"latitude": origin["latitude"]+(destination["latitude"]-origin["latitude"])*i/299,
                   "longitude": origin["longitude"]+(destination["longitude"]-origin["longitude"])*i/299}
                  for i in range(300)]
        return RoadRoute.model_validate({**route.model_dump(mode="json"), "geometry": points})

    routing._onemap_route = offline_route
    return create_app()


@asynccontextmanager
async def bulk_http_cluster(database_url, log_dir, count=50, *, paced=False):
    names = ("fleet", "demand", "facilities", "rebalancing", "dispatch")
    # Give each workload its own database inside the explicitly disposable server.
    database_name = "bulk_" + uuid4().hex
    admin = await asyncpg.connect(database_url)
    await admin.execute('CREATE DATABASE "' + database_name + '"')
    await admin.close()
    parsed = urlsplit(database_url)
    isolated_url = urlunsplit(parsed._replace(path="/"+database_name))
    sockets = [socket.socket() for _ in names]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    urls = {name: f"http://127.0.0.1:{port}" for name, port in zip(names, ports)}
    root = Path(__file__).resolve().parents[1]
    environment = service_test_environment(root, DATABASE_URL=isolated_url,
        **{name.upper()+"_URL": url for name, url in urls.items()},
        ROUTING_PROVIDER="ONEMAP", ONEMAP_ACCESS_TOKEN="offline-test-fixture",
        FACILITIES_SOURCE="MOCK", TRAFFIC_SOURCE="DISABLED", TRAFFIC_SIMULATION_ENABLED="false",
        SIMULATOR_RECOVERY_ENABLED="true", SIMULATOR_RECOVERY_VEHICLE_IDS=",".join(f"bulk-{i:02d}" for i in range(count)),
        SIMULATOR_RECOVERY_DISPATCH_URL=urls["dispatch"], RECOMMENDATION_TTL_SECONDS="120")
    if paced:
        environment.update(SIMULATION_TIME_SCALE="30", DISPATCH_IN_FLIGHT_LIMIT="4")
    processes, logs = [], []
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            for name, port in zip(names, ports):
                log = open(Path(log_dir)/(name+".log"), "w")
                logs.append(log)
                module = ("tests.bulk_http_support:create_bulk_dispatch_app" if name=="dispatch"
                          else "ev_fleet."+name+".app:app")
                service_environment = dict(environment)
                runner = "uvicorn"
                if os.getenv("BULK_PROFILE_SERVICES"):
                    runner = "tests.bulk_http_support"
                    service_environment["BULK_PROFILE_OUTPUT"] = str(Path(log_dir)/(name+".prof"))
                if name == "fleet" and os.getenv("BULK_TRACE_WRITES"):
                    module = "tests.bulk_http_support:create_bulk_fleet_app"
                if name == "facilities" and paced:
                    module = "tests.bulk_http_support:create_bulk_facilities_app"
                args = [sys.executable, "-m", runner, module, "--host", "127.0.0.1", "--port", str(port)]
                if (name == "dispatch" or name == "fleet" and os.getenv("BULK_TRACE_WRITES")
                        or name == "facilities" and paced):
                    args.append("--factory")
                process = subprocess.Popen(args, env=service_environment, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
                processes.append(process)
                deadline = asyncio.get_running_loop().time()+40
                while True:
                    try:
                        if (await client.get(urls[name]+"/health/ready")).status_code==200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None or asyncio.get_running_loop().time()>deadline:
                        raise RuntimeError(name+" failed startup: "+(Path(log_dir)/(name+".log")).read_text())
                    await asyncio.sleep(.1)
        yield urls, isolated_url
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
        for log in logs:
            log.close()
        if os.getenv("BULK_LOG_COPY_TO"):
            shutil.copytree(log_dir, os.environ["BULK_LOG_COPY_TO"], dirs_exist_ok=True)
        admin = await asyncpg.connect(database_url)
        try:
            await admin.execute('DROP DATABASE "' + database_name + '" WITH (FORCE)')
        finally:
            await admin.close()


if __name__ == "__main__":
    # Uvicorn re-raises SIGTERM after graceful shutdown. Turn that final signal
    # into SystemExit so the diagnostic profile is flushed before process exit.
    import cProfile
    import runpy
    import signal

    def stop_profiled_service(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_profiled_service)
    profile = cProfile.Profile()
    profile.enable()
    try:
        sys.argv[0] = "uvicorn"
        runpy.run_module("uvicorn", run_name="__main__")
    finally:
        profile.disable()
        profile.dump_stats(os.environ["BULK_PROFILE_OUTPUT"])


@asynccontextmanager
async def distributed_ev_drivers(urls, directory, *, paced=False):
    import json
    processes, files, logs = [], [], []

    def observe():
        result = []
        for path in files:
            result.append(json.loads(path.read_text()) if path.exists() else None)
        return result

    try:
        for start in range(0, 50, 10):
            path = Path(directory) / f"driver-{start}.json"
            log = open(Path(directory) / f"driver-{start}.log", "w")
            logs.append(log)
            files.append(path)
            args = [sys.executable, "-m", "tests.bulk_driver",
                "--fleet", urls["fleet"], "--dispatch", urls["dispatch"],
                "--state", str(path), "--start", str(start)]
            if paced:
                args += ["--facilities", urls["facilities"], "--latitude", "1.30", "--scale", "30"]
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            processes.append(process)
            deadline = asyncio.get_running_loop().time() + 40
            while True:
                status = observe()[-1]
                if status and status["error"]:
                    raise RuntimeError(status["error"])
                if status and status["ready"]:
                    break
                assert process.poll() is None and asyncio.get_running_loop().time() < deadline, observe()
                await asyncio.sleep(.05)
        yield observe
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
        for log in logs:
            log.close()
