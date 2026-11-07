"""Local supervision and diagnostic reporting, independent of physical telemetry."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import time
from typing import TYPE_CHECKING, Any, Coroutine
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

if TYPE_CHECKING:
    from .commands import MovingNode


class SimulatorHealth:
    def __init__(self, vehicle_id: str, heartbeat_timeout: float = 30.0):
        self.vehicle_id = vehicle_id
        self.process_id = str(uuid4())
        self.heartbeat_timeout = heartbeat_timeout
        self.loops: dict[str, float] = {}
        self.worker: asyncio.Task | None = None
        self.node: MovingNode | None = None
        self.mode = "STARTING"
        self.blocking_reason: str | None = None
        self.changed = asyncio.Event()
        self.checkpoint_revision = 0
        self.checkpoint_at: str | None = None

    def progress(self, loop: str = "execution") -> None:
        self.loops[loop] = time.monotonic()

    def state(self, mode: str, reason: str | None = None) -> None:
        if (self.mode, self.blocking_reason) != (mode, reason):
            self.changed.set()
        self.mode, self.blocking_reason = mode, reason

    def snapshot(self) -> dict:
        age = max((time.monotonic() - tick for tick in self.loops.values()), default=None)
        initialized = bool(self.loops) and self.worker is not None
        alive = bool(
            initialized and self.worker is not None and not self.worker.done()
            and age is not None and age < self.heartbeat_timeout)
        reason = self.blocking_reason
        if self.worker is not None and self.worker.done():
            reason = "EXECUTION_TASK_STOPPED"
        elif age is not None and age >= self.heartbeat_timeout:
            reason = "EXECUTION_HEARTBEAT_EXPIRED"
        runner = getattr(self.node, "checkpoint_runner", None)
        lease = getattr(runner, "lease", None)
        return {
            "vehicle_id": self.vehicle_id, "process_id": self.process_id,
            "initialized": initialized, "live": alive,
            "ready": alive and self.mode in ("RUNNING", "RETRYING", "RECOVERY_WAIT"),
            "mode": self.mode, "blocking_reason": reason,
            "heartbeat_age_seconds": age,
            "heartbeat_timeout_seconds": self.heartbeat_timeout,
            "checkpoint_revision": self.checkpoint_revision,
            "checkpoint_at": self.checkpoint_at,
            "session_id": getattr(self.node, "session", None),
            "generation": lease.generation if lease else None,
        }


def health_app(health: SimulatorHealth) -> FastAPI:
    app = FastAPI(title="Simulator local supervision")

    @app.get("/health/{probe}")
    async def probe(probe: str):
        if probe not in ("startup", "live", "ready"):
            return JSONResponse(status_code=404, content={"error": "UNKNOWN_PROBE"})
        status = health.snapshot()
        key = "initialized" if probe == "startup" else probe
        # Startup also rejects a dead/stalled worker, even if it once started.
        ok = status[key] and status["live"]
        return JSONResponse(status_code=200 if ok else 503, content=status,
                            headers={"Cache-Control": "no-store"})

    return app


async def report_health(health: SimulatorHealth, fleet_url: str) -> None:
    """Publishing never advances the worker heartbeat or physical observed_at."""
    sequence = 0
    async with httpx.AsyncClient(timeout=2) as client:
        while True:
            health.changed.clear()
            payload = {**health.snapshot(), "sequence": sequence}
            sequence += 1
            try:
                response = await asyncio.wait_for(client.put(
                    f"{fleet_url}/api/v1/vehicles/{health.vehicle_id}/simulator-runtime",
                    json=payload), timeout=2)
                response.raise_for_status()
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                logging.debug("vehicle=%s runtime_report=%s", health.vehicle_id, type(exc).__name__)
            try:
                await asyncio.wait_for(health.changed.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


async def run_supervised(health: SimulatorHealth, worker: Coroutine[Any, Any, None],
                         fleet_url: str, port: int) -> None:
    """A failed execution task ends the process; HTTP cannot outlive its worker."""
    server = uvicorn.Server(uvicorn.Config(
        health_app(health), host="0.0.0.0", port=port, access_log=False,
        log_level="warning", lifespan="off"))
    execution = asyncio.create_task(worker, name="simulator-execution")
    health.worker = execution
    serving = asyncio.create_task(server.serve(), name="simulator-health")
    reporting = asyncio.create_task(report_health(health, fleet_url), name="simulator-reporting")
    tasks = (execution, serving, reporting)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        health.state("STOPPED", "EXECUTION_STOPPING")
        server.should_exit = True
        for task in (execution, reporting):
            task.cancel()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
        for task in tasks:
            task.cancel()
