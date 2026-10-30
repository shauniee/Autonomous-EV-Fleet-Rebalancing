import asyncio
from contextlib import asynccontextmanager, suppress
from uuid import UUID
import httpx
from ev_fleet.common.logging import base_app
from ev_fleet.contracts.common import Position
from .routing import road_match_spawn
from ev_fleet.contracts.dispatch import DispatchApproval, CommandAcknowledgement, DispatchEvent, Dispatch, DispatchList, CommandList, BatchApproval, BatchDispatch
from .service import DispatchService


def create_app(settings=None, client=None, clock=None, background=True):
    app = base_app("Dispatch Service")

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=2) as owned:
            service = DispatchService(client or owned, settings, clock)
            app.state.service = service
            app.state.persistence = service.repository
            await service.start()
            worker = asyncio.create_task(service.reconcile_loop()) if background else None
            try:
                yield
            finally:
                if worker:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker
                await service.close()

    app.router.lifespan_context = lifespan

    @app.post("/api/v1/road-matches")
    async def match_idle_spawn(position: Position):
        # The simulator calls this before its first telemetry/session, never
        # while an EV is executing a dispatch. Does not change Fleet state.
        service = app.state.service
        return await road_match_spawn(service.client, service.routing, position.model_dump())

    @app.post("/api/v1/dispatches", status_code=202, response_model=Dispatch)
    async def approve(body: DispatchApproval):
        return await app.state.service.approve(body)

    @app.post("/api/v1/batches", status_code=202, response_model=BatchDispatch)
    async def approve_batch(body: BatchApproval):
        return await app.state.service.approve_batch(body)

    @app.get("/api/v1/batches/{request_id}", response_model=BatchDispatch)
    async def batch(request_id: UUID):
        return await app.state.service.get_batch(request_id)

    @app.get("/api/v1/dispatches", response_model=DispatchList)
    async def dispatches(vehicle_id: str | None = None):
        service = app.state.service
        return {"dispatches": [record.view() for record in service.store.records.values()
                               if vehicle_id is None or record.data["vehicle_id"] == vehicle_id]}

    @app.get("/api/v1/dispatch-planning-state")
    async def planning_state():
        return await app.state.service.planning_snapshot()

    @app.get("/api/v1/dispatches/{dispatch_id}/traffic-check")
    async def traffic_check(dispatch_id: UUID):
        return await app.state.service.traffic_check(dispatch_id)

    @app.get("/api/v1/dispatches/{dispatch_id}", response_model=Dispatch)
    async def dispatch(dispatch_id: UUID):
        return await app.state.service.get(dispatch_id)

    @app.get("/api/v1/vehicles/{vehicle_id}/commands", response_model=CommandList)
    async def commands(vehicle_id: str):
        return await app.state.service.commands(vehicle_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/acknowledgements", response_model=Dispatch)
    async def acknowledge(dispatch_id: UUID, body: CommandAcknowledgement):
        return await app.state.service.acknowledge(dispatch_id, body)

    @app.post("/api/v1/dispatches/{dispatch_id}/events", response_model=Dispatch)
    async def event(dispatch_id: UUID, body: DispatchEvent):
        return await app.state.service.event(dispatch_id, body)

    return app


app = create_app()
