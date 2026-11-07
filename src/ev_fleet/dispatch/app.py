import asyncio
from contextlib import asynccontextmanager, suppress
from uuid import UUID
import httpx
from fastapi import Header
from ev_fleet.common.logging import base_app
from ev_fleet.contracts.common import Position
from .routing import road_match_spawn
from ev_fleet.contracts.crds import CrdsRouteRequest, CrdsRouteEstimate
from ev_fleet.contracts.dispatch import DispatchApproval, CommandAcknowledgement, DispatchEvent, Dispatch, DispatchList, CommandList, BatchApproval, BatchDispatch
from .service import DispatchService
from .recovery import assess_recovery
from .resolution import assess_resolution, reconcile_relocation, request_end, stop_intent
from ev_fleet.contracts.recovery import (RelocationEndRequest, RelocationResolutionAssessment,
    RelocationResolutionOperation, SimulatorStopRequest)
from ev_fleet.contracts.recovery import RecoveryAssessment, RecoveryRequest, RecoveryHandoffRequest, SimulatorSession
from ev_fleet.contracts.dispatch import DispatchApprovalResult, RecoveryOperation


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

    @app.post("/api/v1/dispatches", status_code=202, response_model=DispatchApprovalResult)
    async def approve(body: DispatchApproval):
        service = app.state.service
        await service.approve(body)
        return service.approval_view(body.request_id)

    @app.get("/api/v1/approvals/{request_id}", response_model=DispatchApprovalResult)
    async def approval(request_id: UUID):
        return app.state.service.approval_view(request_id)

    @app.post("/api/v1/approvals/{request_id}/close", response_model=DispatchApprovalResult)
    async def close_approval(request_id: UUID):
        return await app.state.service.close_unstarted(request_id)

    @app.post("/api/v1/batches/{request_id}/items/{recommendation_id}/close", response_model=BatchDispatch)
    async def close_batch_item(request_id: UUID, recommendation_id: UUID):
        return await app.state.service.close_batch_item(request_id, recommendation_id)

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

    @app.post("/api/v1/route-estimates", response_model=CrdsRouteEstimate)
    async def route_estimates(body: CrdsRouteRequest):
        return await app.state.service.estimate_route(body)

    @app.get("/api/v1/dispatches/{dispatch_id}/traffic-check")
    async def traffic_check(dispatch_id: UUID):
        return await app.state.service.traffic_check(dispatch_id)

    @app.get("/api/v1/dispatches/{dispatch_id}/resolution", response_model=RelocationResolutionAssessment)
    async def resolution_assessment(dispatch_id: UUID):
        return await assess_resolution(app.state.service, dispatch_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/resolution", status_code=202,
              response_model=RelocationResolutionOperation)
    async def end_relocation(dispatch_id: UUID, body: RelocationEndRequest):
        return await request_end(app.state.service, dispatch_id, body)

    @app.get("/api/v1/dispatches/{dispatch_id}/resolution/intent", response_model=SimulatorStopRequest)
    async def resolution_intent(dispatch_id: UUID):
        return await stop_intent(app.state.service, dispatch_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/reconcile", response_model=Dispatch)
    async def reconcile_dispatch(dispatch_id: UUID):
        service = app.state.service
        record = service.store.get(dispatch_id)
        async with record.lock:
            service.recovery.ensure_ready()
            try:
                await reconcile_relocation(service, record)
            finally:
                await service.persist()
            return record.view()

    @app.get("/api/v1/dispatches/{dispatch_id}/recovery", response_model=RecoveryAssessment)
    async def recovery_assessment(dispatch_id: UUID):
        return await assess_recovery(app.state.service, dispatch_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/recovery", status_code=202, response_model=RecoveryOperation)
    async def recover(dispatch_id: UUID, body: RecoveryRequest):
        return await app.state.service.recovery.request(dispatch_id, body)

    @app.post("/api/v1/dispatches/{dispatch_id}/recovery/handoff", response_model=SimulatorSession)
    async def recovery_handoff(dispatch_id: UUID, body: RecoveryHandoffRequest):
        return await app.state.service.recovery.handoff(dispatch_id, body)

    @app.get("/api/v1/dispatches/{dispatch_id}", response_model=Dispatch)
    async def dispatch(dispatch_id: UUID):
        return await app.state.service.get(dispatch_id)

    @app.get("/api/v1/vehicles/{vehicle_id}/commands", response_model=CommandList)
    async def commands(vehicle_id: str):
        return await app.state.service.commands(vehicle_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/acknowledgements", response_model=Dispatch)
    async def acknowledge(dispatch_id: UUID, body: CommandAcknowledgement,
                          simulator_generation: int | None = Header(default=None, alias="X-Simulator-Generation"),
                          simulator_session_id: UUID | None = Header(default=None, alias="X-Simulator-Session")):
        return await app.state.service.acknowledge(dispatch_id, body,
            simulator_generation=simulator_generation, simulator_session_id=simulator_session_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/events", response_model=Dispatch)
    async def event(dispatch_id: UUID, body: DispatchEvent,
                    simulator_generation: int | None = Header(default=None, alias="X-Simulator-Generation"),
                    simulator_session_id: UUID | None = Header(default=None, alias="X-Simulator-Session")):
        return await app.state.service.event(dispatch_id, body,
            simulator_generation=simulator_generation, simulator_session_id=simulator_session_id)

    return app


app = create_app()
