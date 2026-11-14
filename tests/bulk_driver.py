"""Independent EV load-driver process for the disposable HTTP harness."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import signal
import ssl
from pathlib import Path

import httpx

from ev_fleet.simulator.commands import MovingNode
from ev_fleet.simulator.health import SimulatorHealth, report_health


async def run(args):
    measurements = {"checkpoint": [], "events": [], "other": []}
    retries, nodes, healths, workers, reporters = [], [], [], [], []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if hasattr(loop, "add_signal_handler"):
        try:
            loop.add_signal_handler(signal.SIGTERM, stop.set)
        except NotImplementedError:
            pass
    original_client = httpx.AsyncClient
    trust = ssl.create_default_context()

    def client(*positional, **options):
        options.setdefault("verify", trust)
        return original_client(*positional, **options)

    httpx.AsyncClient = client

    class Health(SimulatorHealth):
        def state(self, mode, reason=None):
            if mode in ("RETRYING", "RECOVERY_WAIT"):
                retries.append({"vehicle": self.vehicle_id, "mode": mode, "reason": reason})
            super().state(mode, reason)

    async def measure(response):
        await response.aread()
        request = response.request
        kind = ("checkpoint" if request.method == "PUT" and request.url.path.endswith("/simulator-checkpoint")
                else "events" if request.url.path.endswith("/events") else "other")
        measurements[kind].append(response.elapsed.total_seconds() * 1000)

    def publish(error=None):
        payload = {"ready": len(healths) == args.count and all(
            h.mode == "RUNNING" and node.ready.is_set() for h, node in zip(healths, nodes)),
            "retries": retries, "measurements": measurements,
            "blocked": [node.checkpoint_runner.blocked if node.checkpoint_runner else None for node in nodes],
            "error": error}
        temporary = Path(args.state + ".tmp")
        temporary.write_text(json.dumps(payload))
        temporary.replace(args.state)

    async with AsyncExitStack() as clients:
        try:
            for index in range(args.start, args.start + args.count):
                owned = await clients.enter_async_context(client(
                    timeout=2, trust_env=False, event_hooks={"response": [measure]}))
                health = Health(f"bulk-{index:02d}")
                node = MovingNode(args.fleet, args.dispatch, health.vehicle_id,
                    {"latitude": args.latitude, "longitude": 103.8513}, 90,
                    client=owned, road_match_initial=False, checkpointing=True, runtime=health,
                    facilities_url=args.facilities, time_scale=args.scale)
                health.worker = asyncio.create_task(node.run())
                workers.append(health.worker)
                reporters.append(asyncio.create_task(report_health(health, args.fleet)))
                nodes.append(node)
                healths.append(health)
                deadline = loop.time() + 15
                while health.mode != "RUNNING" or not node.ready.is_set():
                    publish()
                    assert loop.time() < deadline, health.vehicle_id
                    await asyncio.sleep(.02)
            while not stop.is_set():
                for worker in workers:
                    if worker.done():
                        worker.result()
                        raise RuntimeError("EV worker stopped unexpectedly")
                publish()
                try:
                    await asyncio.wait_for(stop.wait(), .2)
                except asyncio.TimeoutError:
                    pass
        except BaseException as exc:
            publish(repr(exc))
            raise
        finally:
            for task in workers + reporters:
                task.cancel()
            await asyncio.gather(*workers, *reporters, return_exceptions=True)
            publish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet", required=True)
    parser.add_argument("--dispatch", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--facilities", default=None)
    parser.add_argument("--latitude", type=float, default=1.35)
    parser.add_argument("--scale", type=float, default=10)
    asyncio.run(run(parser.parse_args()))
